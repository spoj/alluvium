from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

from .config import Config
from .store import upsert_task
from .util import atomic_write_json, ensure_dir, iso_now, make_task_id, tree_latest_mtime, append_event

TASK_STATES = ["queued", "running", "worker_done", "integrating", "done", "needs_revision", "needs_human", "failed", "dead_letter", "archived", "lost"]


def ensure_task_dirs(config: Config) -> None:
    # Public, producer-owned intake.
    ensure_dir(config.inbox_path)
    # System-owned stable task artifact directories. A task never moves after claim.
    ensure_dir(config.tasks_path)
    ensure_dir(config.worktrees_path)
    ensure_dir(config.logs_path)


def should_ignore_inbox_name(config: Config, name: str) -> bool:
    return any(name.startswith(p) for p in config.safety.ignore_name_prefixes) or any(
        name.endswith(s) for s in config.safety.ignore_name_suffixes
    )


def is_stable(path: Path, settle_seconds: int) -> bool:
    return (time.time() - tree_latest_mtime(path)) >= settle_seconds


def iter_claimable_inbox(config: Config) -> list[Path]:
    items: list[Path] = []
    if not config.inbox_path.exists():
        return items
    for item in sorted(config.inbox_path.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
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
        if not (config.tasks_path / task_id).exists():
            return task_id
    raise RuntimeError(f"could not reserve unique task id for {original_name!r}")


def claim_inbox_item(config: Config, item: Path) -> Path:
    """Move a free-form inbox folder OR bare file to tasks/<task-id>/input.

    `inbox/` is the only producer-owned area. After claim, the task folder path
    is stable for the lifetime of the task; state lives in SQLite rather than in
    directory names.
    """
    original_name = item.name
    task_id = reserve_task_id(config, original_name)
    staging = config.tasks_path / f".claiming-{task_id}-{uuid.uuid4().hex[:8]}"
    task_final = config.tasks_path / task_id
    input_dir = staging / "input"
    ensure_dir(input_dir)
    try:
        if item.is_dir():
            # Preserve folder contents under input/. Producer-supplied .agent is
            # therefore just untrusted input/input/.agent, not runtime state.
            for child in list(item.iterdir()):
                os.rename(child, input_dir / child.name)
            item.rmdir()
        else:
            os.rename(item, input_dir / original_name)
        prepare_agent_subtree(config, staging, original_name=original_name, task_id=task_id)
        shutil.move(str(staging), str(task_final))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    append_event(task_final, config.system_dir, "claimed_from_inbox", original_name=original_name, task_id=task_id)
    upsert_task(config, task_final, "queued")
    return task_final


def prepare_agent_subtree(config: Config, task_dir: Path, *, original_name: str, task_id: str) -> Path:
    return ensure_runtime_subtree(config, task_dir, original_name=original_name, task_id=task_id, preserve_identity=False)


def ensure_runtime_subtree(
    config: Config,
    task_dir: Path,
    *,
    original_name: str | None = None,
    task_id: str | None = None,
    preserve_identity: bool = True,
) -> Path:
    agent_dir = task_dir / config.reserved_dir
    system_dir = task_dir / config.system_dir
    ensure_dir(agent_dir)
    ensure_dir(system_dir)
    # Worker-facing skeleton: anything the worker is expected to write into.
    for name in ["outputs", "scratch", "logs", "spawned_tasks", "discovery"]:
        ensure_dir(agent_dir / name)
    # Harness-only skeleton: bookkeeping the worker should not touch.
    for name in ["repo", "attempts"]:
        ensure_dir(system_dir / name)
    identity_path = system_dir / "identity.json"
    if not preserve_identity or not identity_path.exists():
        identity = {
            "task_id": task_id or task_dir.name,
            "original_name": original_name or task_dir.name,
            "claimed_at": iso_now(),
            "claim_token": str(uuid.uuid4()),
            "reserved_dir": config.reserved_dir,
            "system_dir": config.system_dir,
        }
        atomic_write_json(identity_path, identity)
    return agent_dir


def claim_revision_task(config: Config, item: Path) -> Path:
    """Mark an existing stable task folder as claimed for revision."""
    ensure_runtime_subtree(config, item, preserve_identity=True)
    append_event(item, config.system_dir, "claimed_for_revision", task_id=item.name)
    return item


def move_task(config: Config, task_dir: Path, state: str) -> Path:
    """Record a logical state transition without moving the stable task folder."""
    if state not in TASK_STATES:
        raise ValueError(f"unknown task state: {state}")
    append_event(task_dir, config.system_dir, f"state_{state}")
    return task_dir


def create_inbox_task(config: Config, name: str, files: dict[str, str]) -> Path:
    """Create an internal free-form inbox task atomically."""
    staging = config.inbox_path / f".{name}.{uuid.uuid4().hex}.tmp"
    ensure_dir(staging)
    for rel, text in files.items():
        p = staging / rel
        ensure_dir(p.parent)
        p.write_text(text, encoding="utf-8")
    final = config.inbox_path / name
    if final.exists():
        final = config.inbox_path / f"{name}-{uuid.uuid4().hex[:6]}"
    os.rename(staging, final)
    return final
