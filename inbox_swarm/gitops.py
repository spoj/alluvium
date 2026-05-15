from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .config import Config
from .util import CommandError, atomic_write_json, atomic_write_text, ensure_dir, iso_now, run_cmd


def git_env(config: Config) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": config.git.author_name,
        "GIT_AUTHOR_EMAIL": config.git.author_email,
        "GIT_COMMITTER_NAME": config.git.author_name,
        "GIT_COMMITTER_EMAIL": config.git.author_email,
    }


def git(config: Config, *args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = run_cmd(["git", *args], cwd=cwd or config.repo_path, env=git_env(config), check=check)
    return proc.stdout.strip()


def ensure_git_repo(config: Config) -> None:
    if not (config.repo_path / ".git").exists():
        raise RuntimeError(f"repo_path is not a Git repository: {config.repo_path}")
    git(config, "rev-parse", "--is-inside-work-tree")


def current_commit(config: Config, ref: str) -> str:
    return git(config, "rev-parse", ref)


def branch_name_for_task(task_id: str) -> str:
    safe = task_id.replace("/", "-")
    return f"task/{safe}"


def worktree_path_for_task(config: Config, task_id: str) -> Path:
    return config.worktrees_path / task_id


def create_worktree(config: Config, *, task_id: str) -> tuple[str, Path, str]:
    ensure_git_repo(config)
    branch = branch_name_for_task(task_id)
    worktree = worktree_path_for_task(config, task_id)
    ensure_dir(config.worktrees_path)
    base = config.integration.base_branch
    base_commit = current_commit(config, base)

    if worktree.exists():
        shutil.rmtree(worktree)
    # Unique task IDs should make branch collisions impossible. Delete stale branch defensively.
    existing = run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=config.repo_path, check=False)
    if existing.returncode == 0:
        git(config, "branch", "-D", branch)
    git(config, "worktree", "add", "-b", branch, str(worktree), base)
    return branch, worktree, base_commit


def write_repo_metadata(config: Config, task_dir: Path, *, branch: str, worktree: Path, base_commit: str) -> None:
    repo_dir = task_dir / config.reserved_dir / "repo"
    ensure_dir(repo_dir)
    atomic_write_text(repo_dir / "branch.txt", branch + "\n")
    atomic_write_text(repo_dir / "worktree.txt", str(worktree) + "\n")
    atomic_write_text(repo_dir / "base_commit.txt", base_commit + "\n")


def porcelain_status(worktree: Path) -> str:
    proc = run_cmd(["git", "status", "--porcelain"], cwd=worktree, check=True)
    return proc.stdout


def auto_commit_if_dirty(config: Config, *, worktree: Path, task_id: str) -> bool:
    if not config.git.auto_commit_worker_changes:
        return False
    if not porcelain_status(worktree).strip():
        return False
    run_cmd(["git", "add", "-A"], cwd=worktree, env=git_env(config), check=True)
    proc = run_cmd(
        ["git", "commit", "-m", f"Task {task_id}: agent changes"],
        cwd=worktree,
        env=git_env(config),
        check=False,
    )
    if proc.returncode != 0:
        # Usually means there was nothing to commit after all.
        status = porcelain_status(worktree)
        if status.strip():
            raise CommandError(["git", "commit"], proc.returncode, proc.stdout, proc.stderr)
        return False
    return True


def branch_head(config: Config, branch: str) -> str:
    return current_commit(config, branch)


def branch_commit_count(config: Config, *, base_commit: str, branch: str) -> int:
    out = git(config, "rev-list", "--count", f"{base_commit}..{branch}")
    return int(out or "0")


def branch_has_diff(config: Config, *, base_commit: str, branch: str) -> bool:
    proc = run_cmd(["git", "diff", "--quiet", base_commit, branch], cwd=config.repo_path, check=False)
    return proc.returncode != 0


def diffstat(config: Config, *, base_commit: str, branch: str) -> str:
    proc = run_cmd(["git", "diff", "--stat", base_commit, branch], cwd=config.repo_path, check=False)
    return proc.stdout


def patch(config: Config, *, base_commit: str, branch: str) -> str:
    proc = run_cmd(["git", "diff", base_commit, branch], cwd=config.repo_path, check=False)
    return proc.stdout


def finalize_worker_branch(config: Config, task_dir: Path, *, branch: str, worktree: Path, base_commit: str) -> dict[str, Any]:
    auto_commit_if_dirty(config, worktree=worktree, task_id=task_dir.name)
    head = branch_head(config, branch)
    has_changes = branch_has_diff(config, base_commit=base_commit, branch=branch)
    count = branch_commit_count(config, base_commit=base_commit, branch=branch)

    repo_dir = task_dir / config.reserved_dir / "repo"
    ensure_dir(repo_dir)
    atomic_write_text(repo_dir / "head_commit.txt", head + "\n")
    atomic_write_text(repo_dir / "diffstat.txt", diffstat(config, base_commit=base_commit, branch=branch))
    if has_changes:
        atomic_write_text(repo_dir / "patch.diff", patch(config, base_commit=base_commit, branch=branch))

    integration = {
        "status": "pending" if has_changes else "noop",
        "has_repo_changes": has_changes,
        "branch": branch,
        "base_commit": base_commit,
        "head_commit": head,
        "commit_count": count,
        "updated_at": iso_now(),
    }
    atomic_write_json(task_dir / config.reserved_dir / "integration.json", integration)
    return integration


def main_is_clean(config: Config) -> bool:
    return not git(config, "status", "--porcelain").strip()


def run_integration_tests(config: Config) -> tuple[bool, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    for command in config.integration.run_tests:
        proc = run_cmd(command, cwd=config.repo_path, env=git_env(config), check=False, shell=True)
        ok = proc.returncode == 0
        results.append(
            {
                "command": command,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-8000:],
                "stderr": proc.stderr[-8000:],
            }
        )
        if not ok:
            return False, results
    return True, results


def integration_json_path(config: Config, task_dir: Path) -> Path:
    return task_dir / config.reserved_dir / "integration.json"


def integrate_task(config: Config, task_dir: Path) -> dict[str, Any]:
    """Serially integrate a done task's branch into the base branch.

    Returns the new integration status payload. On conflicts/test failures, the
    base worktree is reset to a clean state and the task is marked blocked.
    """
    integration_path = integration_json_path(config, task_dir)
    import json

    integration = json.loads(integration_path.read_text(encoding="utf-8"))
    branch = integration.get("branch")
    base_commit = integration.get("base_commit")
    if not branch or not base_commit:
        integration.update({"status": "noop", "has_repo_changes": False, "updated_at": iso_now()})
        atomic_write_json(integration_path, integration)
        return integration

    if not branch_has_diff(config, base_commit=base_commit, branch=branch):
        integration.update({"status": "noop", "has_repo_changes": False, "updated_at": iso_now()})
        atomic_write_json(integration_path, integration)
        cleanup_task_worktree_and_branch(config, task_dir.name, branch)
        return integration

    if not main_is_clean(config):
        integration.update(
            {
                "status": "blocked",
                "reason": "base repo worktree is not clean",
                "blocked_at": iso_now(),
                "updated_at": iso_now(),
            }
        )
        atomic_write_json(integration_path, integration)
        return integration

    base = config.integration.base_branch
    git(config, "checkout", base)

    merge = run_cmd(["git", "merge", "--squash", branch], cwd=config.repo_path, env=git_env(config), check=False)
    if merge.returncode != 0:
        conflict_text = f"STDOUT:\n{merge.stdout}\n\nSTDERR:\n{merge.stderr}\n"
        atomic_write_text(task_dir / config.reserved_dir / "repo" / "integration_conflict.txt", conflict_text)
        run_cmd(["git", "reset", "--hard", "HEAD"], cwd=config.repo_path, env=git_env(config), check=False)
        integration.update(
            {
                "status": "blocked",
                "reason": "merge conflict",
                "merge_stdout": merge.stdout[-8000:],
                "merge_stderr": merge.stderr[-8000:],
                "blocked_at": iso_now(),
                "updated_at": iso_now(),
            }
        )
        atomic_write_json(integration_path, integration)
        return integration

    tests_ok, test_results = run_integration_tests(config)
    atomic_write_json(task_dir / config.reserved_dir / "repo" / "integration_tests.json", test_results)
    if not tests_ok:
        run_cmd(["git", "reset", "--hard", "HEAD"], cwd=config.repo_path, env=git_env(config), check=False)
        integration.update(
            {
                "status": "blocked",
                "reason": "integration tests failed",
                "tests": test_results,
                "blocked_at": iso_now(),
                "updated_at": iso_now(),
            }
        )
        atomic_write_json(integration_path, integration)
        return integration

    commit = run_cmd(
        ["git", "commit", "-m", f"Integrate task {task_dir.name}"],
        cwd=config.repo_path,
        env=git_env(config),
        check=False,
    )
    if commit.returncode != 0:
        # If squash merge staged no diff, treat as noop; otherwise block.
        if main_is_clean(config):
            integration.update({"status": "noop", "has_repo_changes": False, "updated_at": iso_now()})
            atomic_write_json(integration_path, integration)
            cleanup_task_worktree_and_branch(config, task_dir.name, branch)
            return integration
        run_cmd(["git", "reset", "--hard", "HEAD"], cwd=config.repo_path, env=git_env(config), check=False)
        integration.update(
            {
                "status": "blocked",
                "reason": "git commit failed",
                "commit_stdout": commit.stdout[-8000:],
                "commit_stderr": commit.stderr[-8000:],
                "blocked_at": iso_now(),
                "updated_at": iso_now(),
            }
        )
        atomic_write_json(integration_path, integration)
        return integration

    merge_commit = current_commit(config, "HEAD")
    integration.update(
        {
            "status": "merged",
            "has_repo_changes": True,
            "merge_commit": merge_commit,
            "merged_at": iso_now(),
            "tests": test_results,
            "updated_at": iso_now(),
        }
    )
    atomic_write_json(integration_path, integration)
    cleanup_task_worktree_and_branch(config, task_dir.name, branch)
    return integration


def cleanup_task_worktree_and_branch(config: Config, task_id: str, branch: str) -> None:
    if not config.integration.cleanup_after_merge:
        return
    worktree = worktree_path_for_task(config, task_id)
    if worktree.exists():
        run_cmd(["git", "worktree", "remove", "--force", str(worktree)], cwd=config.repo_path, env=git_env(config), check=False)
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
    run_cmd(["git", "branch", "-D", branch], cwd=config.repo_path, env=git_env(config), check=False)


def list_pending_integrations(config: Config) -> list[Path]:
    done = config.tasks_path / "done"
    tasks: list[Path] = []
    if not done.exists():
        return tasks
    for task in sorted(done.iterdir()):
        p = integration_json_path(config, task)
        if not p.exists():
            continue
        try:
            import json

            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("status") == "pending":
            tasks.append(task)
    return tasks


def init_repo_if_needed(config: Config) -> None:
    ensure_dir(config.repo_path)
    if not (config.repo_path / ".git").exists():
        run_cmd(["git", "init", "-b", config.integration.base_branch], cwd=config.repo_path, env=git_env(config), check=True)
        ensure_dir(config.repo_path / "knowledge" / "curated")
        ensure_dir(config.repo_path / "knowledge" / "inbox")
        ensure_dir(config.repo_path / "knowledge" / "sources")
        ensure_dir(config.repo_path / "skills" / "approved")
        ensure_dir(config.repo_path / "skills" / "proposed")
        for rel in [
            "knowledge/curated/.gitkeep",
            "knowledge/inbox/.gitkeep",
            "knowledge/sources/.gitkeep",
            "skills/approved/.gitkeep",
            "skills/proposed/.gitkeep",
        ]:
            (config.repo_path / rel).write_text("", encoding="utf-8")
        (config.repo_path / "AGENTS.md").write_text(_default_repo_agents_md(), encoding="utf-8")
        run_cmd(["git", "add", "-A"], cwd=config.repo_path, env=git_env(config), check=True)
        run_cmd(["git", "commit", "-m", "Initialize Inbox Swarm repository"], cwd=config.repo_path, env=git_env(config), check=True)


def _default_repo_agents_md() -> str:
    return """# Repository Agent Conventions

This repository is maintained by Inbox Swarm workers.

- Write per-task durable knowledge notes under `knowledge/inbox/`.
- Write source metadata under `knowledge/sources/`.
- Write generated tools under `skills/proposed/`.
- Treat `skills/approved/` as read-only unless performing an explicit skill-review task.
- Rewrite `knowledge/curated/` only for explicit synthesis/curation tasks.
- Never commit raw private source files unless explicitly requested.
- Preserve provenance and citations for knowledge derived from task inputs.
"""
