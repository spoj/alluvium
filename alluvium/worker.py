from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from .config import Config
from .gitops import create_worktree, finalize_worker_branch, write_repo_metadata
from .prompts import worker_prompt
from .util import atomic_write_json, atomic_write_text, append_event, iso_now, render_command


async def launch_worker(config: Config, task_dir: Path, *, git_lock: asyncio.Lock) -> dict[str, Any]:
    task_id = task_dir.name
    agent_dir = task_dir / config.reserved_dir

    async with git_lock:
        branch, worktree, base_commit = create_worktree(config, task_id=task_id, task_dir=task_dir)
        write_repo_metadata(config, task_dir, branch=branch, worktree=worktree, base_commit=base_commit)

    prompt = worker_prompt(config, task_id=task_id, task_dir=task_dir, worktree=worktree, branch=branch)
    prompt_file = agent_dir / "prompt.md"
    atomic_write_text(prompt_file, prompt)

    mapping = {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "agent_dir": str(agent_dir),
        "worktree": str(worktree),
        "branch": branch,
        "prompt_file": str(prompt_file),
    }
    command = render_command(config.agent.command, mapping)
    atomic_write_json(agent_dir / "command.json", {"command": command, "started_at": iso_now()})
    append_event(task_dir, "worker_starting", command=command, branch=branch, worktree=str(worktree))

    stdout_path = agent_dir / "logs" / "stdout.log"
    stderr_path = agent_dir / "logs" / "stderr.log"
    env = {
        **os.environ,
        "ALLUVIUM_TASK_ID": task_id,
        "ALLUVIUM_TASK_DIR": str(task_dir),
        "ALLUVIUM_AGENT_DIR": str(agent_dir),
        "ALLUVIUM_WORKTREE": str(worktree),
        "ALLUVIUM_BRANCH": branch,
        "ALLUVIUM_PROMPT_FILE": str(prompt_file),
    }

    with stdout_path.open("ab") as stdout_fh, stderr_path.open("ab") as stderr_fh:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(worktree),
                env=env,
                stdout=stdout_fh,
                stderr=stderr_fh,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            append_event(task_dir, "worker_launch_failed", error=str(exc))
            atomic_write_json(
                agent_dir / "result.json",
                {
                    "status": "failed",
                    "summary": f"Worker command could not be launched: {exc}",
                    "needs_human": False,
                    "repo_changed": False,
                    "external_effects": False,
                },
            )
            return {"exit_code": 127, "branch": branch, "worktree": str(worktree), "base_commit": base_commit}

        atomic_write_json(agent_dir / "process.json", {"pid": proc.pid, "started_at": iso_now()})
        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=config.agent.timeout_seconds)
        except TimeoutError:
            append_event(task_dir, "worker_timeout", timeout_seconds=config.agent.timeout_seconds)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=20)
            except TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                await proc.wait()
            exit_code = -9
        except asyncio.CancelledError:
            append_event(task_dir, "worker_process_cancelled", pid=proc.pid)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                await proc.wait()
            raise

    append_event(task_dir, "worker_exited", exit_code=exit_code)
    atomic_write_json(agent_dir / "process.json", {"exit_code": exit_code, "finished_at": iso_now()})

    # Capture/commit repository state even if the worker failed, so diagnostics are preserved.
    try:
        async with git_lock:
            integration = finalize_worker_branch(config, task_dir, branch=branch, worktree=worktree, base_commit=base_commit)
    except Exception as exc:
        append_event(task_dir, "finalize_branch_failed", error=str(exc))
        integration = {
            "status": "blocked",
            "has_repo_changes": True,
            "branch": branch,
            "base_commit": base_commit,
            "error": str(exc),
            "updated_at": iso_now(),
        }
        atomic_write_json(agent_dir / "integration.json", integration)

    return {
        "exit_code": exit_code,
        "branch": branch,
        "worktree": str(worktree),
        "base_commit": base_commit,
        "integration": integration,
    }


def task_needs_human(config: Config, task_dir: Path) -> bool:
    return (task_dir / config.reserved_dir / "needs_human.md").exists()


def ensure_result_files(config: Config, task_dir: Path, *, status: str, summary: str) -> None:
    agent_dir = task_dir / config.reserved_dir
    result_md = agent_dir / "result.md"
    result_json = agent_dir / "result.json"
    if not result_md.exists():
        atomic_write_text(result_md, f"# Result\n\nStatus: {status}\n\n{summary}\n")
    if not result_json.exists():
        atomic_write_json(
            result_json,
            {
                "status": status,
                "summary": summary,
                "outputs": [],
                "repo_changed": False,
                "external_effects": False,
                "needs_human": status == "needs_human",
            },
        )
