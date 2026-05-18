from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .fsqueue import claim_inbox_item, claim_revision_task, ensure_task_dirs, iter_claimable_inbox, move_task
from .gitops import cleanup_task_worktree_and_branch, ensure_git_repo, integrate_task, list_pending_integrations
from .util import append_event, atomic_write_json, ensure_dir, iso_now, read_json
from .store import db_path, init_store, mark_task_state, task_row, task_rows, task_counts, tasks_by_state, upsert_task
from .worker import ensure_result_files, launch_worker, task_needs_human


class DaemonLock:
    """Small cross-platform single-runner lock.

    This intentionally avoids Unix-only fcntl. The lock file is created with
    O_EXCL; if it already exists and the recorded pid is dead, it is treated as
    stale and replaced. This is sufficient for a local runner whose state lives
    on a normal local filesystem.
    """

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        ensure_dir(self.path.parent)
        payload = f"{os.getpid()}\n"
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc:
                existing_pid = self._read_pid()
                if existing_pid and pid_is_running(existing_pid):
                    raise RuntimeError(f"another alluvium runner appears to be active: pid {existing_pid}, lock {self.path}") from exc
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            self.acquired = True
            return
        raise RuntimeError(f"could not acquire alluvium lock: {self.path}")

    def _read_pid(self) -> int | None:
        try:
            text = self.path.read_text(encoding="utf-8").strip().splitlines()[0]
            return int(text)
        except Exception:
            return None

    def release(self) -> None:
        if self.acquired:
            try:
                if self._read_pid() == os.getpid():
                    self.path.unlink(missing_ok=True)
            finally:
                self.acquired = False


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows can raise OSError/WinError 87 for stale or invalid pids.
        return False


def read_daemon_pid(config: Config) -> int | None:
    data = read_json(config.pid_path, None)
    if isinstance(data, dict):
        try:
            return int(data.get("pid"))
        except (TypeError, ValueError):
            return None
    try:
        text = config.pid_path.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (FileNotFoundError, ValueError):
        return None


class AlluviumDaemon:
    def __init__(self, config: Config, *, config_path: Path | None = None):
        self.config = config
        self.config_path = config_path
        self.stop_event = asyncio.Event()
        self.worker_slots = asyncio.Semaphore(config.safety.max_workers)
        self.git_lock = asyncio.Lock()
        self.main_repo_lock = asyncio.Lock()
        self.running_tasks: set[str] = set()
        self.worker_tasks: set[asyncio.Task[Any]] = set()
        self.lock = DaemonLock(config.lock_path)

    async def run(self) -> None:
        self.lock.acquire()
        try:
            ensure_task_dirs(self.config)
            init_store(self.config)
            ensure_git_repo(self.config)
            self.write_pid_file()
            self.reconcile_on_startup()
            self.install_signal_handlers()
            await asyncio.gather(
                self.coordinator_loop(),
                self.integrator_loop(),
                self.janitor_loop(),
            )
        finally:
            await self.shutdown_workers()
            self.remove_pid_file()
            self.lock.release()

    def write_pid_file(self) -> None:
        ensure_dir(self.config.daemon_dir)
        atomic_write_json(
            self.config.pid_path,
            {
                "pid": os.getpid(),
                "started_at": iso_now(),
                "config_path": str(self.config_path) if self.config_path else None,
                "root": str(self.config.root),
            },
        )

    def remove_pid_file(self) -> None:
        try:
            pid = read_daemon_pid(self.config)
            if pid == os.getpid():
                self.config.pid_path.unlink(missing_ok=True)
        except Exception:
            pass

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:
                pass
        if hasattr(signal, "SIGHUP"):
            try:
                loop.add_signal_handler(signal.SIGHUP, self.reload_config)
            except NotImplementedError:
                pass

    def request_stop(self) -> None:
        print("[daemon] stop requested", flush=True)
        self.stop_event.set()

    def reload_config(self) -> None:
        if not self.config_path:
            print("[daemon] reload requested but no config path is known", flush=True)
            return
        try:
            new_config = load_config(self.config_path)
            # Keep daemon identity/path stable while still reloading operational settings.
            immutable_changed = any(
                getattr(new_config, name) != getattr(self.config, name)
                for name in ["root", "repo_path", "inbox_path", "tasks_path", "worktrees_path", "logs_path", "reserved_dir"]
            )
            if immutable_changed:
                print(
                    "[daemon] config reloaded; root/repo/inbox/tasks/worktrees/logs/reserved_dir changes require restart and were ignored",
                    flush=True,
                )
                new_config.root = self.config.root
                new_config.repo_path = self.config.repo_path
                new_config.inbox_path = self.config.inbox_path
                new_config.tasks_path = self.config.tasks_path
                new_config.worktrees_path = self.config.worktrees_path
                new_config.logs_path = self.config.logs_path
                new_config.reserved_dir = self.config.reserved_dir
            old_max = self.config.safety.max_workers
            self.config = new_config
            if self.config.safety.max_workers != old_max:
                print("[daemon] max_workers changes require daemon restart", flush=True)
            ensure_task_dirs(self.config)
            ensure_git_repo(self.config)
            print(f"[daemon] config reloaded from {self.config_path}", flush=True)
        except Exception as exc:
            print(f"[daemon] config reload failed: {exc}", flush=True)

    async def sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def reconcile_on_startup(self) -> None:
        """Repair simple SQLite/filesystem disagreements before scheduling.

        Invariant: inbox/ is unclaimed external input; every claimed task should
        have a stable directory under tasks/ and a SQLite row. SQLite remains
        authoritative for state, but this pass detects missing/orphaned artifacts
        and interrupted in-flight states conservatively.
        """
        self.reconcile_claiming_dirs()
        self.reconcile_missing_task_dirs()
        self.reconcile_orphan_task_dirs()
        self.reconcile_interrupted_states()

    def reconcile_claiming_dirs(self) -> None:
        quarantine = self.config.tasks_path / ".quarantine"
        ensure_dir(quarantine)
        for path in list(self.config.tasks_path.iterdir()) if self.config.tasks_path.exists() else []:
            if not path.is_dir() or not path.name.startswith(".claiming-"):
                continue
            dest = quarantine / f"{path.name}-{iso_now().replace(':', '').replace('.', '')}"
            try:
                os.rename(path, dest)
            except OSError:
                try:
                    import shutil

                    shutil.move(str(path), str(dest))
                except Exception:
                    pass

    def reconcile_missing_task_dirs(self) -> None:
        for row in task_rows(self.config):
            state = str(row.get("state") or "")
            if state in {"lost", "archived"}:
                continue
            task_dir = Path(str(row.get("task_dir") or ""))
            if task_dir.exists():
                continue
            task_id = str(row.get("id"))
            mark_task_state(self.config, task_id, "lost")
            record_path = self.config.daemon_dir / "lost_tasks.jsonl"
            try:
                from .util import append_jsonl

                append_jsonl(record_path, {"ts": iso_now(), "task_id": task_id, "missing_task_dir": str(task_dir)})
            except Exception:
                pass

    def reconcile_orphan_task_dirs(self) -> None:
        if not self.config.tasks_path.exists():
            return
        for task in sorted(self.config.tasks_path.iterdir()):
            if not task.is_dir() or task.name.startswith("."):
                continue
            if task_row(self.config, task.name):
                continue
            state = self.infer_orphan_state(task)
            try:
                append_event(task, "reconstructed_missing_db_row", inferred_state=state)
                upsert_task(self.config, task, state)
            except Exception:
                quarantine = self.config.tasks_path / ".quarantine"
                ensure_dir(quarantine)
                try:
                    os.rename(task, quarantine / f"orphan-{task.name}")
                except Exception:
                    pass

    def infer_orphan_state(self, task: Path) -> str:
        integration = read_json(task / self.config.reserved_dir / "integration.json", {})
        if isinstance(integration, dict):
            status = integration.get("status")
            if status in {"merged", "noop"}:
                return "done"
            if status == "pending":
                return "worker_done"
            if status == "needs_revision":
                return "needs_revision"
            if status == "blocked":
                return "needs_human"
        if (task / self.config.reserved_dir / "needs_human.md").exists():
            return "needs_human"
        result = read_json(task / self.config.reserved_dir / "result.json", {})
        if isinstance(result, dict):
            status = result.get("status")
            if status == "failed":
                return "failed"
            if status == "needs_human":
                return "needs_human"
            if status == "succeeded":
                return "done"
        return "queued"

    def reconcile_interrupted_states(self) -> None:
        for state in ["running", "integrating"]:
            for task in tasks_by_state(self.config, state):
                if not task.exists():
                    continue
                try:
                    append_event(task, "startup_recovery_interrupted", previous_state=state)
                    ensure_result_files(
                        self.config,
                        task,
                        status="failed",
                        summary=f"Runner restarted while this task was {state}; marked failed conservatively.",
                    )
                    failed = move_task(self.config, task, "failed")
                    upsert_task(self.config, failed, "failed")
                except Exception:
                    # Last resort: leave it for manual inspection.
                    pass

    async def coordinator_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.start_available_workers()
            except Exception as exc:
                print(f"[coordinator] error: {exc}", flush=True)
            await self.sleep_or_stop(self.config.safety.scan_interval_seconds)

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
            upsert_task(self.config, task_dir, "running")
            self.running_tasks.add(task_dir.name)
            worker_task = asyncio.create_task(self.run_one_worker(task_dir))
            self.worker_tasks.add(worker_task)
            worker_task.add_done_callback(self.worker_tasks.discard)

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
                failed = move_task(self.config, task_dir, "failed")
                upsert_task(self.config, failed, "failed")
            elif task_needs_human(self.config, task_dir):
                ensure_result_files(
                    self.config,
                    task_dir,
                    status="needs_human",
                    summary="Worker requested human input or approval.",
                )
                needs_human = move_task(self.config, task_dir, "needs_human")
                upsert_task(self.config, needs_human, "needs_human")
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
                    done = move_task(self.config, task_dir, "done")
                    upsert_task(self.config, done, "done")
                else:
                    worker_done = move_task(self.config, task_dir, "worker_done")
                    upsert_task(self.config, worker_done, "worker_done")
        except asyncio.CancelledError:
            append_event(task_dir, "worker_cancelled_by_daemon_shutdown")
            raise
        except Exception as exc:
            try:
                append_event(task_dir, "worker_supervisor_error", error=str(exc))
                ensure_result_files(self.config, task_dir, status="failed", summary=f"Supervisor error: {exc}")
                failed = move_task(self.config, task_dir, "failed")
                upsert_task(self.config, failed, "failed")
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
            await self.sleep_or_stop(self.config.safety.integrator_interval_seconds)

    async def integrate_ready_done_tasks(self, *, limit: int | None = None) -> int:
        count = 0
        for task in list_pending_integrations(self.config):
            if limit is not None and count >= limit:
                break
            async with self.main_repo_lock:
                upsert_task(self.config, task, "integrating")
                append_event(task, "integration_started")
                status = await asyncio.to_thread(integrate_task, self.config, task)
                append_event(task, "integration_finished", status=status.get("status"), reason=status.get("reason"))
                if status.get("status") == "needs_revision":
                    needs_revision = move_task(self.config, task, "needs_revision")
                    upsert_task(self.config, needs_revision, "needs_revision")
                elif status.get("status") in {"merged", "noop"}:
                    done = move_task(self.config, task, "done")
                    upsert_task(self.config, done, "done")
                elif status.get("status") == "blocked" and self.config.integration.move_unrevisionable_to_needs_human:
                    needs = task / self.config.reserved_dir / "needs_human.md"
                    needs.write_text(
                        "# Integration blocked\n\n"
                        f"Reason: {status.get('reason', 'unknown')}\n\n"
                        "Inspect `.agent/repo/` and `.agent/integration.json`. Resolve manually or move this task to `needs_revision/` to ask a worker to amend it.\n",
                        encoding="utf-8",
                    )
                    needs_human = move_task(self.config, task, "needs_human")
                    upsert_task(self.config, needs_human, "needs_human")
            count += 1
        return count

    def iter_revision_tasks(self) -> list[Path]:
        return [p for p in tasks_by_state(self.config, "needs_revision") if p.exists()]

    async def janitor_loop(self) -> None:
        while not self.stop_event.is_set():
            # Placeholder for future lease expiry, stale worktree pruning, metrics, etc.
            await self.sleep_or_stop(self.config.safety.janitor_interval_seconds)

    async def shutdown_workers(self) -> None:
        if not self.worker_tasks:
            return
        grace = max(0, int(getattr(self.config.safety, "shutdown_grace_seconds", 10)))
        print(f"[daemon] waiting up to {grace}s for {len(self.worker_tasks)} worker(s) to finish", flush=True)
        done, pending = await asyncio.wait(self.worker_tasks, timeout=grace)
        if pending:
            print(f"[daemon] cancelling {len(pending)} worker(s)", flush=True)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def run_once(self) -> None:
        """Drain currently claimable work, then integrate all pending branches.

        This is intentionally batch-oriented: it keeps starting low-concurrency
        workers until the claimable inbox/revision set is empty, then waits for
        the final batch and runs serialized integration. New files that arrive
        after the last scan are left for the next run/daemon tick.
        """
        self.lock.acquire()
        try:
            ensure_task_dirs(self.config)
            init_store(self.config)
            ensure_git_repo(self.config)
            self.reconcile_on_startup()
            while True:
                await self.start_available_workers()
                while self.running_tasks:
                    await asyncio.sleep(0.1)
                if not self.iter_revision_tasks() and not iter_claimable_inbox(self.config):
                    break
            if self.config.integration.enabled:
                await self.integrate_ready_done_tasks(limit=None)
        finally:
            self.lock.release()


def status_summary(config: Config) -> dict[str, Any]:
    ensure_task_dirs(config)
    indexed_counts = task_counts(config)
    tasks = dict(indexed_counts)
    tasks["inbox"] = len([p for p in config.inbox_path.iterdir() if not p.name.startswith(".")]) if config.inbox_path.exists() else 0
    tasks["claimed"] = len([p for p in config.tasks_path.iterdir() if p.is_dir() and not p.name.startswith(".")]) if config.tasks_path.exists() else 0
    pending_integrations = [p.name for p in list_pending_integrations(config)]
    pid = read_daemon_pid(config)
    return {
        "root": str(config.root),
        "repo_path": str(config.repo_path),
        "inbox_path": str(config.inbox_path),
        "tasks_path": str(config.tasks_path),
        "state_db": str(db_path(config)),
        "indexed_tasks": indexed_counts,
        "daemon": {
            "pid": pid,
            "running": pid_is_running(pid) if pid is not None else False,
            "pid_file": str(config.pid_path),
            "log_file": str(config.daemon_log_path),
        },
        "tasks": tasks,
        "pending_integrations": pending_integrations,
    }
