from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

from .config import Config
from .util import atomic_write_json, ensure_dir, iso_now, make_task_id, tree_latest_mtime, append_event

TASK_STATES = ["inbox", "running", "needs_human", "done", "failed", "dead_letter"]


def ensure_task_dirs(config: Config) -> None:
    for state in TASK_STATES:
        ensure_dir(config.tasks_path / state)
    ensure_dir(config.worktrees_path)
    ensure_dir(config.logs_path)


def should_ignore_inbox_name(config: Config, name: str) -> bool:
    return any(name.startswith(p) for p in config.safety.ignore_name_prefixes) or any(
        name.endswith(s) for s in config.safety.ignore_name_suffixes
    )


def is_stable(path: Path, settle_seconds: int) -> bool:
    return (time.time() - tree_latest_mtime(path)) >= settle_seconds


def iter_claimable_inbox(config: Config) -> list[Path]:
    inbox = config.tasks_path / "inbox"
    items: list[Path] = []
    if not inbox.exists():
        return items
    for item in sorted(inbox.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if should_ignore_inbox_name(config, item.name):
            continue
        if not item.exists():
            continue
        if is_stable(item, config.safety.inbox_settle_seconds):
            items.append(item)
    return items


def reserve_task_id(config: Config, original_name: str) -> str:
    # Include timestamp + random suffix; still loop defensively.
    for _ in range(100):
        task_id = make_task_id(original_name)
        if not any((config.tasks_path / state / task_id).exists() for state in TASK_STATES):
            return task_id
    raise RuntimeError(f"could not reserve unique task id for {original_name!r}")


def claim_inbox_item(config: Config, item: Path) -> Path:
    """Move a free-form inbox folder OR bare file to running/<unique-task-id>.

    Directories are atomically renamed. Bare files are wrapped in a newly-created
    task directory, so a single dropped file behaves exactly like a folder task.
    """
    original_name = item.name
    task_id = reserve_task_id(config, original_name)
    running_final = config.tasks_path / "running" / task_id

    if item.is_dir():
        os.rename(item, running_final)
    else:
        staging = config.tasks_path / "running" / f".{task_id}.claiming-{uuid.uuid4().hex}"
        ensure_dir(staging)
        try:
            os.rename(item, staging / original_name)
            os.rename(staging, running_final)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    prepare_agent_subtree(config, running_final, original_name=original_name, task_id=task_id)
    append_event(running_final, "claimed_from_inbox", original_name=original_name, task_id=task_id)
    return running_final


def prepare_agent_subtree(config: Config, task_dir: Path, *, original_name: str, task_id: str) -> Path:
    agent_dir = task_dir / config.reserved_dir
    if agent_dir.exists():
        # Treat a producer-supplied reserved dir as input, not trusted runtime state.
        quarantined = task_dir / f"_producer_agent_dir_{uuid.uuid4().hex[:8]}"
        os.rename(agent_dir, quarantined)
    ensure_dir(agent_dir)
    for name in ["outputs", "scratch", "logs", "repo", "effects", "spawned_tasks"]:
        ensure_dir(agent_dir / name)
    atomic_write_json(
        agent_dir / "identity.json",
        {
            "task_id": task_id,
            "original_name": original_name,
            "claimed_at": iso_now(),
            "claim_token": str(uuid.uuid4()),
            "reserved_dir": config.reserved_dir,
        },
    )
    return agent_dir


def move_task(config: Config, task_dir: Path, state: str) -> Path:
    if state not in TASK_STATES:
        raise ValueError(f"unknown task state: {state}")
    dest = config.tasks_path / state / task_dir.name
    if dest.exists():
        # Should not happen with unique internal IDs, but do not overwrite task history.
        dest = config.tasks_path / state / f"{task_dir.name}-{uuid.uuid4().hex[:6]}"
    append_event(task_dir, f"moving_to_{state}")
    os.rename(task_dir, dest)
    append_event(dest, f"moved_to_{state}")
    return dest


def create_inbox_task(config: Config, name: str, files: dict[str, str]) -> Path:
    """Create an internal free-form inbox task atomically.

    Used for system-created synthesis tasks and examples. The public API remains:
    create a folder in tasks/inbox.
    """
    inbox = config.tasks_path / "inbox"
    staging = inbox / f".{name}.{uuid.uuid4().hex}.tmp"
    ensure_dir(staging)
    for rel, text in files.items():
        p = staging / rel
        ensure_dir(p.parent)
        p.write_text(text, encoding="utf-8")
    final = inbox / name
    if final.exists():
        final = inbox / f"{name}-{uuid.uuid4().hex[:6]}"
    os.rename(staging, final)
    return final
