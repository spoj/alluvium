from __future__ import annotations

import asyncio
import json
from pathlib import Path

from alluvium.config import default_config_text, load_config
from alluvium.daemon import AlluviumDaemon
from alluvium.fsqueue import ensure_task_dirs
from alluvium.gitops import init_repo_if_needed


def make_system(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    text = default_config_text(tmp_path)
    text = text.replace("inbox_settle_seconds = 5", "inbox_settle_seconds = 0")
    text = text.replace("max_workers = 2", "max_workers = 4")
    config_path.write_text(text, encoding="utf-8")
    config = load_config(config_path)
    ensure_task_dirs(config)
    init_repo_if_needed(config)
    return config


def test_bare_file_inbox_item_is_wrapped_and_completed(tmp_path: Path):
    config = make_system(tmp_path)
    (config.tasks_path / "inbox" / "note.txt").write_text("hello", encoding="utf-8")

    asyncio.run(AlluviumDaemon(config).run_once())

    done = [p for p in (config.tasks_path / "done").iterdir() if p.is_dir()]
    assert len(done) == 1
    task = done[0]
    assert task.name.endswith("note.txt-" + task.name.rsplit("-", 1)[-1]) or "note.txt" in task.name
    assert (task / "note.txt").read_text(encoding="utf-8") == "hello"
    inventory = json.loads((task / ".agent" / "outputs" / "inventory.json").read_text(encoding="utf-8"))
    assert inventory == [{"path": "note.txt", "size": 5}]
    integration = json.loads((task / ".agent" / "integration.json").read_text(encoding="utf-8"))
    assert integration["status"] == "noop"
    assert integration["has_repo_changes"] is False


def test_directory_duplicate_names_get_unique_internal_ids(tmp_path: Path):
    config = make_system(tmp_path)

    first = config.tasks_path / "inbox" / "same-name"
    first.mkdir()
    (first / "a.txt").write_text("a", encoding="utf-8")
    asyncio.run(AlluviumDaemon(config).run_once())

    second = config.tasks_path / "inbox" / "same-name"
    second.mkdir()
    (second / "b.txt").write_text("b", encoding="utf-8")
    asyncio.run(AlluviumDaemon(config).run_once())

    done = sorted([p.name for p in (config.tasks_path / "done").iterdir() if p.is_dir()])
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

    task = config.tasks_path / "inbox" / "repo-task"
    task.mkdir()
    (task / "request.md").write_text("add note", encoding="utf-8")

    asyncio.run(AlluviumDaemon(config).run_once())

    assert (config.repo_path / "note.md").read_text(encoding="utf-8") == "# Note\n"
    done = [p for p in (config.tasks_path / "done").iterdir() if p.is_dir()]
    assert len(done) == 1
    integration = json.loads((done[0] / ".agent" / "integration.json").read_text(encoding="utf-8"))
    assert integration["status"] == "merged"
    assert integration["has_repo_changes"] is True
    assert "merge_commit" in integration


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

    task = config.tasks_path / "inbox" / "needs-revision"
    task.mkdir()
    (task / "request.md").write_text("make required.txt", encoding="utf-8")

    asyncio.run(AlluviumDaemon(config).run_once())

    revisions = [p for p in (config.tasks_path / "needs_revision").iterdir() if p.is_dir()]
    assert len(revisions) == 1
    revision_task = revisions[0]
    assert (revision_task / ".agent" / "revision_request.md").exists()
    integration = json.loads((revision_task / ".agent" / "integration.json").read_text(encoding="utf-8"))
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
    done = [p for p in (config.tasks_path / "done").iterdir() if p.is_dir()]
    assert len(done) == 1
    integration = json.loads((done[0] / ".agent" / "integration.json").read_text(encoding="utf-8"))
    assert integration["status"] == "merged"
    assert integration["revision_round"] == 1
