from __future__ import annotations

import os
import shutil
import uuid
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


def create_worktree(config: Config, *, task_id: str, task_dir: Path | None = None) -> tuple[str, Path, str]:
    """Create or reuse the task branch/worktree.

    New inbox tasks get a fresh branch from the base branch. Tasks returned for
    revision keep their existing branch and worktree so the worker can amend the
    previous attempt instead of starting over.
    """
    ensure_git_repo(config)
    branch = branch_name_for_task(task_id)
    worktree = worktree_path_for_task(config, task_id)
    ensure_dir(config.worktrees_path)
    base = config.integration.base_branch
    base_commit = current_commit(config, base)

    if task_dir is not None:
        prior_base = task_dir / config.reserved_dir / "repo" / "base_commit.txt"
        if prior_base.exists():
            base_commit = prior_base.read_text(encoding="utf-8").strip() or base_commit

    existing = run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=config.repo_path, check=False)
    if existing.returncode == 0:
        if not worktree.exists():
            git(config, "worktree", "add", str(worktree), branch)
        return branch, worktree, base_commit

    if worktree.exists():
        shutil.rmtree(worktree)
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

    prior = {}
    prior_path = task_dir / config.reserved_dir / "integration.json"
    if prior_path.exists():
        try:
            import json

            prior = json.loads(prior_path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
    integration = {
        "status": "pending" if has_changes else "noop",
        "has_repo_changes": has_changes,
        "branch": branch,
        "base_commit": base_commit,
        "head_commit": head,
        "commit_count": count,
        "revision_round": int(prior.get("revision_round", 0)),
        "updated_at": iso_now(),
    }
    atomic_write_json(task_dir / config.reserved_dir / "integration.json", integration)
    return integration


def main_is_clean(config: Config) -> bool:
    return not git(config, "status", "--porcelain").strip()


def run_integration_tests(config: Config, *, cwd: Path | None = None) -> tuple[bool, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    for command in config.integration.run_tests:
        proc = run_cmd(command, cwd=cwd or config.repo_path, env=git_env(config), check=False, shell=True)
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


def _return_for_revision_or_block(
    config: Config,
    task_dir: Path,
    integration: dict[str, Any],
    *,
    reason: str,
    revisionable: bool,
    details: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    revision_round = int(integration.get("revision_round", 0))
    can_revise = revisionable and config.integration.return_blocked_for_revision and revision_round < config.integration.max_revision_rounds
    status = "needs_revision" if can_revise else "blocked"
    if can_revise:
        revision_round += 1
    integration.update(
        {
            "status": status,
            "reason": reason,
            "revision_round": revision_round,
            "updated_at": iso_now(),
        }
    )
    if status == "blocked":
        integration["blocked_at"] = iso_now()
    if extra:
        integration.update(extra)
    feedback = task_dir / config.reserved_dir / "revision_request.md"
    if status == "needs_revision":
        feedback.write_text(
            "# Revision requested by integrator\n\n"
            f"Reason: {reason}\n\n"
            f"Revision round: {revision_round} of {config.integration.max_revision_rounds}\n\n"
            "Please amend the existing task branch to address this feedback. The daemon will re-run a worker on this same task and branch.\n\n"
            + ("## Details\n\n" + details if details else ""),
            encoding="utf-8",
        )
    atomic_write_json(integration_json_path(config, task_dir), integration)
    return integration


def integrate_task(config: Config, task_dir: Path) -> dict[str, Any]:
    """Serially integrate a done task branch via a temporary worktree.

    The canonical repo worktree is kept clean while merge attempts and tests run
    elsewhere. Only after the temporary integration branch passes do we
    fast-forward the configured base branch.
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
        return _return_for_revision_or_block(
            config,
            task_dir,
            integration,
            reason="base repo worktree is not clean",
            revisionable=False,
        )

    base = config.integration.base_branch
    base_head = current_commit(config, base)
    token = uuid.uuid4().hex[:8]
    integration_branch = f"alluvium/integrate/{task_dir.name}-{token}"
    integration_worktree = config.worktrees_path / f".integrate-{task_dir.name}-{token}"

    def cleanup_integration_workspace() -> None:
        run_cmd(["git", "worktree", "remove", "--force", str(integration_worktree)], cwd=config.repo_path, env=git_env(config), check=False)
        if integration_worktree.exists():
            shutil.rmtree(integration_worktree, ignore_errors=True)
        run_cmd(["git", "branch", "-D", integration_branch], cwd=config.repo_path, env=git_env(config), check=False)

    try:
        if integration_worktree.exists():
            shutil.rmtree(integration_worktree)
        run_cmd(
            ["git", "worktree", "add", "-b", integration_branch, str(integration_worktree), base],
            cwd=config.repo_path,
            env=git_env(config),
            check=True,
        )

        merge = run_cmd(["git", "merge", "--squash", branch], cwd=integration_worktree, env=git_env(config), check=False)
        if merge.returncode != 0:
            conflict_text = f"STDOUT:\n{merge.stdout}\n\nSTDERR:\n{merge.stderr}\n"
            atomic_write_text(task_dir / config.reserved_dir / "repo" / "integration_conflict.txt", conflict_text)
            return _return_for_revision_or_block(
                config,
                task_dir,
                integration,
                reason="merge conflict",
                revisionable=True,
                details=conflict_text,
                extra={"merge_stdout": merge.stdout[-8000:], "merge_stderr": merge.stderr[-8000:]},
            )

        tests_ok, test_results = run_integration_tests(config, cwd=integration_worktree)
        atomic_write_json(task_dir / config.reserved_dir / "repo" / "integration_tests.json", test_results)
        if not tests_ok:
            return _return_for_revision_or_block(
                config,
                task_dir,
                integration,
                reason="integration tests failed",
                revisionable=True,
                details=json.dumps(test_results, indent=2),
                extra={"tests": test_results},
            )

        commit = run_cmd(
            ["git", "commit", "-m", f"Integrate task {task_dir.name}"],
            cwd=integration_worktree,
            env=git_env(config),
            check=False,
        )
        if commit.returncode != 0:
            # If squash merge staged no diff, treat as noop; otherwise block.
            if not porcelain_status(integration_worktree).strip():
                integration.update({"status": "noop", "has_repo_changes": False, "updated_at": iso_now()})
                atomic_write_json(integration_path, integration)
                cleanup_task_worktree_and_branch(config, task_dir.name, branch)
                return integration
            return _return_for_revision_or_block(
                config,
                task_dir,
                integration,
                reason="git commit failed",
                revisionable=False,
                details=f"STDOUT:\n{commit.stdout}\n\nSTDERR:\n{commit.stderr}\n",
                extra={"commit_stdout": commit.stdout[-8000:], "commit_stderr": commit.stderr[-8000:]},
            )

        merge_commit = run_cmd(["git", "rev-parse", "HEAD"], cwd=integration_worktree, env=git_env(config), check=True).stdout.strip()
        if current_commit(config, base) != base_head:
            return _return_for_revision_or_block(
                config,
                task_dir,
                integration,
                reason="base branch advanced during integration",
                revisionable=True,
                details=f"Base was {base_head}; now {current_commit(config, base)}. Re-run against the updated base.",
            )

        git(config, "checkout", base)
        ff = run_cmd(["git", "merge", "--ff-only", integration_branch], cwd=config.repo_path, env=git_env(config), check=False)
        if ff.returncode != 0:
            return _return_for_revision_or_block(
                config,
                task_dir,
                integration,
                reason="fast-forward integration failed",
                revisionable=False,
                details=f"STDOUT:\n{ff.stdout}\n\nSTDERR:\n{ff.stderr}\n",
                extra={"ff_stdout": ff.stdout[-8000:], "ff_stderr": ff.stderr[-8000:]},
            )

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
    finally:
        cleanup_integration_workspace()


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
    tasks: list[Path] = []
    if not config.tasks_path.exists():
        return tasks
    for task in sorted(config.tasks_path.iterdir()):
        if not task.is_dir() or task.name.startswith("."):
            continue
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
        (config.repo_path / "AGENTS.md").write_text(_default_repo_agents_md(), encoding="utf-8")
        run_cmd(["git", "add", "-A"], cwd=config.repo_path, env=git_env(config), check=True)
        run_cmd(["git", "commit", "-m", "Initialize Alluvium repository"], cwd=config.repo_path, env=git_env(config), check=True)


def _default_repo_agents_md() -> str:
    return """# Repository Agent Conventions

This durable repository is maintained by Alluvium workers.

Workers may change code, docs, knowledge, tools, tests, configuration, or any
other project files when the task warrants it. Make the branch correct as a
whole; do not force changes into a special proposal/knowledge topology unless a
task or this file says so.

General expectations:

- Keep changes focused on the task.
- Add or update tests/docs when appropriate.
- Preserve provenance for facts derived from task inputs.
- Do not commit raw private source files unless explicitly requested.
- Never merge to `main`; the integrator/maintainer does that serially.
"""
