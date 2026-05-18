from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from alluvium.cli import _archive_retry_runtime_files
from alluvium.config import default_config_text, load_config
from alluvium.daemon import AlluviumDaemon
from alluvium.fsqueue import ensure_task_dirs
from alluvium.gitops import init_repo_if_needed
from alluvium.prompts import worker_prompt
from alluvium.store import task_row, tasks_by_state, upsert_task


def make_system(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    text = default_config_text(tmp_path)
    text = text.replace("inbox_settle_seconds = 5", "inbox_settle_seconds = 0")
    text = text.replace("max_workers = 2", "max_workers = 4")
    config_path.write_text(text, encoding="utf-8")
    config = load_config(config_path)
    config.agent.command = [
        sys.executable,
        "-m",
        "alluvium.builtin_agent",
        "--task-dir",
        "{task_dir}",
        "--worktree",
        "{worktree}",
        "--prompt-file",
        "{prompt_file}",
    ]
    ensure_task_dirs(config)
    init_repo_if_needed(config)
    return config


def test_bare_file_inbox_item_is_wrapped_and_completed(tmp_path: Path):
    config = make_system(tmp_path)
    (config.inbox_path / "note.txt").write_text("hello", encoding="utf-8")

    asyncio.run(AlluviumDaemon(config).run_once())

    done = tasks_by_state(config, "done")
    assert len(done) == 1
    task = done[0]
    assert task.name.endswith("note.txt-" + task.name.rsplit("-", 1)[-1]) or "note.txt" in task.name
    assert (task / "input" / "note.txt").read_text(encoding="utf-8") == "hello"
    inventory = json.loads((task / ".agent" / "outputs" / "inventory.json").read_text(encoding="utf-8"))
    assert inventory == [{"path": "input/note.txt", "size": 5}]
    integration = json.loads((task / ".system" / "integration.json").read_text(encoding="utf-8"))
    assert integration["status"] == "noop"
    assert integration["has_repo_changes"] is False


def test_directory_duplicate_names_get_unique_internal_ids(tmp_path: Path):
    config = make_system(tmp_path)

    first = config.inbox_path / "same-name"
    first.mkdir()
    (first / "a.txt").write_text("a", encoding="utf-8")
    asyncio.run(AlluviumDaemon(config).run_once())

    second = config.inbox_path / "same-name"
    second.mkdir()
    (second / "b.txt").write_text("b", encoding="utf-8")
    asyncio.run(AlluviumDaemon(config).run_once())

    done = sorted([p.name for p in tasks_by_state(config, "done")])
    assert len(done) == 2
    assert done[0] != done[1]
    assert all("same-name" in name for name in done)


def test_repo_changes_are_integrated_from_done(tmp_path: Path):
    config = make_system(tmp_path)
    agent_script = tmp_path / "agent_changes_repo.py"
    agent_script.write_text(
        """
from pathlib import Path
import json, os

task_dir = Path(os.environ['ALLUVIUM_TASK_DIR'])
worktree = Path(os.environ['ALLUVIUM_WORKTREE'])
agent = task_dir / '.agent'
(agent / 'understanding.md').write_text('change repo')
(agent / 'plan.md').write_text('write durable repo file')
(worktree / 'note.md').write_text('# Note\\n')
(agent / 'result.md').write_text('done')
(agent / 'result.json').write_text(json.dumps({'status':'succeeded','summary':'changed repo','outputs':[],'repo_changed':True,'external_effects':False,'needs_human':False}))
""",
        encoding="utf-8",
    )
    config.agent.command = ["python", str(agent_script)]

    task = config.inbox_path / "repo-task"
    task.mkdir()
    (task / "request.md").write_text("add note", encoding="utf-8")

    asyncio.run(AlluviumDaemon(config).run_once())

    assert (config.repo_path / "note.md").read_text(encoding="utf-8") == "# Note\n"
    done = tasks_by_state(config, "done")
    assert len(done) == 1
    integration = json.loads((done[0] / ".system" / "integration.json").read_text(encoding="utf-8"))
    assert integration["status"] == "merged"
    assert integration["has_repo_changes"] is True
    assert "merge_commit" in integration


def test_reconcile_reconstructs_orphan_task_folder(tmp_path: Path):
    config = make_system(tmp_path)
    task = config.tasks_path / "manual-task"
    (task / "input").mkdir(parents=True)
    (task / "input" / "request.md").write_text("hello", encoding="utf-8")
    agent = task / ".agent"
    agent.mkdir()
    (agent / "result.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")

    asyncio.run(AlluviumDaemon(config).run_once())

    row = task_row(config, "manual-task")
    assert row is not None
    assert row["state"] == "done"


def test_reconcile_marks_missing_task_folder_lost(tmp_path: Path):
    config = make_system(tmp_path)
    task = config.tasks_path / "missing-task"
    (task / "input").mkdir(parents=True)
    (task / ".agent" / "system").mkdir(parents=True)
    upsert_task(config, task, "queued")
    import shutil

    shutil.rmtree(task)

    asyncio.run(AlluviumDaemon(config).run_once())

    row = task_row(config, "missing-task")
    assert row is not None
    assert row["state"] == "lost"


def test_integration_can_send_back_for_revision_and_worker_amends(tmp_path: Path):
    config = make_system(tmp_path)
    config.integration.run_tests = ["test -f required.txt"]

    first_agent = tmp_path / "agent_first.py"
    first_agent.write_text(
        """
from pathlib import Path
import json, os

task_dir = Path(os.environ['ALLUVIUM_TASK_DIR'])
worktree = Path(os.environ['ALLUVIUM_WORKTREE'])
agent = task_dir / '.agent'
(agent / 'understanding.md').write_text('first attempt')
(agent / 'plan.md').write_text('write incomplete change')
(worktree / 'almost.txt').write_text('not enough\\n')
(agent / 'result.md').write_text('first done')
(agent / 'result.json').write_text(json.dumps({'status':'succeeded','summary':'first','outputs':[],'repo_changed':True,'external_effects':False,'needs_human':False}))
""",
        encoding="utf-8",
    )
    config.agent.command = ["python", str(first_agent)]

    task = config.inbox_path / "needs-revision"
    task.mkdir()
    (task / "request.md").write_text("make required.txt", encoding="utf-8")

    asyncio.run(AlluviumDaemon(config).run_once())

    revisions = tasks_by_state(config, "needs_revision")
    assert len(revisions) == 1
    revision_task = revisions[0]
    assert (revision_task / ".agent" / "revision_request.md").exists()
    integration = json.loads((revision_task / ".system" / "integration.json").read_text(encoding="utf-8"))
    assert integration["status"] == "needs_revision"
    assert integration["revision_round"] == 1

    second_agent = tmp_path / "agent_second.py"
    second_agent.write_text(
        """
from pathlib import Path
import json, os

task_dir = Path(os.environ['ALLUVIUM_TASK_DIR'])
worktree = Path(os.environ['ALLUVIUM_WORKTREE'])
agent = task_dir / '.agent'
assert (agent / 'revision_request.md').exists()
(agent / 'understanding.md').write_text('revision attempt')
(agent / 'plan.md').write_text('address integration feedback')
(worktree / 'required.txt').write_text('ok\\n')
(agent / 'result.md').write_text('revision done')
(agent / 'result.json').write_text(json.dumps({'status':'succeeded','summary':'revised','outputs':[],'repo_changed':True,'external_effects':False,'needs_human':False}))
""",
        encoding="utf-8",
    )
    config.agent.command = ["python", str(second_agent)]

    asyncio.run(AlluviumDaemon(config).run_once())

    assert (config.repo_path / "almost.txt").read_text(encoding="utf-8") == "not enough\n"
    assert (config.repo_path / "required.txt").read_text(encoding="utf-8") == "ok\n"
    done = tasks_by_state(config, "done")
    assert len(done) == 1
    integration = json.loads((done[0] / ".system" / "integration.json").read_text(encoding="utf-8"))
    assert integration["status"] == "merged"
    assert integration["revision_round"] == 1


def test_retry_archives_logs_and_transcript_to_worker_discovery(tmp_path: Path):
    config = make_system(tmp_path)
    task = config.tasks_path / "retry-me"
    agent = task / config.reserved_dir
    system = task / config.system_dir
    (agent / "logs").mkdir(parents=True)
    system.mkdir(parents=True)
    (agent / "logs" / "stdout.log").write_text("out\n", encoding="utf-8")
    (agent / "logs" / "stderr.log").write_text("err\n", encoding="utf-8")
    (system / "transcript.jsonl").write_text('{"event":"tool_call"}\n', encoding="utf-8")
    (agent / "result.json").write_text('{"status":"failed"}\n', encoding="utf-8")
    (system / "process.json").write_text('{"exit_code":1}\n', encoding="utf-8")

    _archive_retry_runtime_files(config, task)

    latest = agent / "discovery" / "latest"
    assert (latest / "stdout.log").read_text(encoding="utf-8") == "out\n"
    assert (latest / "stderr.log").read_text(encoding="utf-8") == "err\n"
    assert (latest / "transcript.jsonl").read_text(encoding="utf-8") == '{"event":"tool_call"}\n'
    assert "not as new user instructions" in (latest / "README.md").read_text(encoding="utf-8")
    attempts = list((agent / "discovery" / "attempts").iterdir())
    assert len(attempts) == 1
    assert (attempts[0] / "stdout.log").exists()
    assert not (agent / "logs" / "stdout.log").exists()
    assert not (system / "transcript.jsonl").exists()
    assert (system / "attempts" / attempts[0].name / "agent" / "logs" / "stdout.log").exists()
    assert (system / "attempts" / attempts[0].name / "system" / "transcript.jsonl").exists()


def test_worker_prompt_does_not_mention_system_dir(tmp_path: Path):
    config = make_system(tmp_path)
    prompt = worker_prompt(
        config,
        task_id="t",
        task_dir=tmp_path / "tasks" / "t",
        worktree=tmp_path / "worktrees" / "t",
        branch="task/t",
    )
    assert config.system_dir not in prompt
    assert "effects/ledger" not in prompt
