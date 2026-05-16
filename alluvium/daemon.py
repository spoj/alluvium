from __future__ import annotations

import asyncio
import fcntl
import os
import signal
from pathlib import Path
from typing import Any

from .config import Config
from .fsqueue import claim_inbox_item, claim_revision_task, ensure_task_dirs, iter_claimable_inbox, move_task
from .gitops import cleanup_task_worktree_and_branch, ensure_git_repo, integrate_task, list_pending_integrations
from .util import append_event, ensure_dir
from .worker import ensure_result_files, launch_worker, task_needs_human


class DaemonLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> None:
        ensure_dir(self.path.parent)
        self.fh = self.path.open("w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another alluvium daemon appears to be running: {self.path}") from exc
        self.fh.write(str(os.getpid()) + "\n")
        self.fh.flush()

    def release(self) -> None:
        if self.fh:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


class AlluviumDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.stop_event = asyncio.Event()
        self.worker_slots = asyncio.Semaphore(config.safety.max_workers)
        self.git_lock = asyncio.Lock()
        self.main_repo_lock = asyncio.Lock()
        self.running_tasks: set[str] = set()
        self.lock = DaemonLock(config.lock_path)

    async def run(self) -> None:
        self.lock.acquire()
        try:
            ensure_task_dirs(self.config)
            ensure_git_repo(self.config)
            self.reconcile_on_startup()
            self.install_signal_handlers()
            await asyncio.gather(
                self.coordinator_loop(),
                self.integrator_loop(),
                self.janitor_loop(),
            )
        finally:
            self.lock.release()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass

    def reconcile_on_startup(self) -> None:
        # Conservative recovery: tasks in running were interrupted. Move to failed.
        running = self.config.tasks_path / "running"
        if not running.exists():
            return
        for task in list(running.iterdir()):
            if not task.is_dir() or task.name.startswith("."):
                continue
            try:
                append_event(task, "startup_recovery_failed_running", reason="daemon restarted while task was running")
                ensure_result_files(
                    self.config,
                    task,
                    status="failed",
                    summary="Daemon restarted while this task was running; moved to failed conservatively.",
                )
                move_task(self.config, task, "failed")
            except Exception:
                # Last resort: leave it for manual inspection.
                pass

    async def coordinator_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.start_available_workers()
            except Exception as exc:
                print(f"[coordinator] error: {exc}", flush=True)
            await asyncio.sleep(self.config.safety.scan_interval_seconds)

    async def start_available_workers(self) -> None:
        while not self.stop_event.is_set():
            if self.worker_slots.locked():
                return
            revision_items = self.iter_revision_tasks()
            inbox_items = iter_claimable_inbox(self.config)
            if revision_items:
                item = revision_items[0]
                claim = claim_revision_task
            elif inbox_items:
                item = inbox_items[0]
                claim = claim_inbox_item
            else:
                return
            try:
                task_dir = claim(self.config, item)
            except FileNotFoundError:
                continue
            except Exception as exc:
                print(f"[coordinator] failed to claim {item}: {exc}", flush=True)
                continue
            await self.worker_slots.acquire()
            self.running_tasks.add(task_dir.name)
            asyncio.create_task(self.run_one_worker(task_dir))

    async def run_one_worker(self, task_dir: Path) -> None:
        try:
            result = await launch_worker(self.config, task_dir, git_lock=self.git_lock)
            exit_code = int(result.get("exit_code", 1))
            if exit_code != 0:
                ensure_result_files(
                    self.config,
                    task_dir,
                    status="failed",
                    summary=f"Worker exited with code {exit_code}.",
                )
                move_task(self.config, task_dir, "failed")
            elif task_needs_human(self.config, task_dir):
                ensure_result_files(
                    self.config,
                    task_dir,
                    status="needs_human",
                    summary="Worker requested human input or approval.",
                )
                move_task(self.config, task_dir, "needs_human")
            else:
                ensure_result_files(
                    self.config,
                    task_dir,
                    status="succeeded",
                    summary="Worker completed.",
                )
                integration = result.get("integration") or {}
                if integration.get("status") == "noop" and integration.get("branch"):
                    async with self.git_lock:
                        cleanup_task_worktree_and_branch(self.config, task_dir.name, str(integration["branch"]))
                move_task(self.config, task_dir, "done")
        except Exception as exc:
            try:
                append_event(task_dir, "worker_supervisor_error", error=str(exc))
                ensure_result_files(self.config, task_dir, status="failed", summary=f"Supervisor error: {exc}")
                move_task(self.config, task_dir, "failed")
            except Exception:
                print(f"[worker-supervisor] unrecoverable error for {task_dir}: {exc}", flush=True)
        finally:
            self.running_tasks.discard(task_dir.name)
            self.worker_slots.release()

    async def integrator_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.config.integration.enabled:
                try:
                    await self.integrate_ready_done_tasks(limit=1)
                except Exception as exc:
                    print(f"[integrator] error: {exc}", flush=True)
            await asyncio.sleep(self.config.safety.integrator_interval_seconds)

    async def integrate_ready_done_tasks(self, *, limit: int | None = None) -> int:
        count = 0
        for task in list_pending_integrations(self.config):
            if limit is not None and count >= limit:
                break
            async with self.main_repo_lock:
                append_event(task, "integration_started")
                status = await asyncio.to_thread(integrate_task, self.config, task)
                append_event(task, "integration_finished", status=status.get("status"), reason=status.get("reason"))
                if status.get("status") == "needs_revision":
                    move_task(self.config, task, "needs_revision")
                elif status.get("status") == "blocked" and self.config.integration.move_unrevisionable_to_needs_human:
                    needs = task / self.config.reserved_dir / "needs_human.md"
                    needs.write_text(
                        "# Integration blocked\n\n"
                        f"Reason: {status.get('reason', 'unknown')}\n\n"
                        "Inspect `.agent/repo/` and `.agent/integration.json`. Resolve manually or move this task to `needs_revision/` to ask a worker to amend it.\n",
                        encoding="utf-8",
                    )
                    move_task(self.config, task, "needs_human")
            count += 1
        return count

    def iter_revision_tasks(self) -> list[Path]:
        revision_dir = self.config.tasks_path / "needs_revision"
        if not revision_dir.exists():
            return []
        return sorted([p for p in revision_dir.iterdir() if p.is_dir() and not p.name.startswith(".")])

    async def janitor_loop(self) -> None:
        while not self.stop_event.is_set():
            # Placeholder for future lease expiry, stale worktree pruning, metrics, etc.
            await asyncio.sleep(self.config.safety.janitor_interval_seconds)

    async def run_once(self) -> None:
        """Useful for tests/debugging: claim/run currently claimable tasks and integrate once."""
        ensure_task_dirs(self.config)
        ensure_git_repo(self.config)
        await self.start_available_workers()
        # Wait for all started worker tasks.
        while self.running_tasks:
            await asyncio.sleep(0.1)
        if self.config.integration.enabled:
            await self.integrate_ready_done_tasks(limit=None)


def status_summary(config: Config) -> dict[str, Any]:
    ensure_task_dirs(config)
    tasks = {}
    for state in ["inbox", "running", "needs_revision", "needs_human", "done", "failed", "dead_letter"]:
        d = config.tasks_path / state
        tasks[state] = len([p for p in d.iterdir() if not p.name.startswith(".")]) if d.exists() else 0
    pending_integrations = [p.name for p in list_pending_integrations(config)]
    return {
        "root": str(config.root),
        "repo_path": str(config.repo_path),
        "tasks": tasks,
        "pending_integrations": pending_integrations,
    }
