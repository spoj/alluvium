from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import default_config_text, load_config
from .daemon import AlluviumDaemon, pid_is_running, read_daemon_pid, status_summary
from .fsqueue import ensure_task_dirs
from .gitops import init_repo_if_needed
from .store import init_store, mark_task_state, task_rows
from .util import ensure_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alluvium", description="Free-form task inbox daemon for coding agents.")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize an Alluvium root directory.")
    init.add_argument("root", nargs="?", default=".", help="Root directory to initialize.")
    init.add_argument("--force", action="store_true", help="Overwrite config.toml if it exists.")

    serve = sub.add_parser("serve", help="Run the local Alluvium runner in the foreground.")
    serve.add_argument("--config", default="config.toml", help="Path to config.toml.")

    daemon = sub.add_parser("daemon", help="Compatibility wrapper: start the runner in the background by default.")
    daemon.add_argument("--config", default="config.toml", help="Path to config.toml.")
    daemon.add_argument("--foreground", action="store_true", help="Run in the foreground instead of spawning a background runner.")

    stop = sub.add_parser("stop-daemon", help="Stop the background runner.")
    stop.add_argument("--config", default="config.toml")
    stop.add_argument("--timeout", type=float, default=20.0, help="Seconds to wait for graceful shutdown.")
    stop.add_argument("--force", action="store_true", help="Send SIGKILL if the daemon does not stop before timeout.")

    reload_cmd = sub.add_parser("reload", help="Ask the background daemon to reload config.toml.")
    reload_cmd.add_argument("--config", default="config.toml")

    once = sub.add_parser("run-once", help="Claim current inbox tasks, run workers, and integrate once.")
    once.add_argument("--config", default="config.toml")
    once.add_argument("--ignore-settle", action="store_true", help="Process inbox items immediately, ignoring inbox_settle_seconds.")

    integrate = sub.add_parser("integrate-once", help="Integrate all pending done-task branches once.")
    integrate.add_argument("--config", default="config.toml")

    status = sub.add_parser("status", help="Print JSON status summary.")
    status.add_argument("--config", default="config.toml")

    retry = sub.add_parser("retry", help="Queue an existing claimed task to run again.")
    retry.add_argument("task", help="Task id or unique id prefix.")
    retry.add_argument("--config", default="config.toml")
    retry.add_argument("--from-any-state", action="store_true", help="Allow retrying tasks that are not failed/needs_revision/needs_human/lost/done.")

    example = sub.add_parser("example-task", help="Drop an example task into inbox/.")
    example.add_argument("--config", default="config.toml")
    example.add_argument("name", nargs="?", default="hello-alluvium")

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    ensure_dir(root)
    config_path = root / "config.toml"
    if config_path.exists() and not args.force:
        print(f"config exists: {config_path} (use --force to overwrite)", file=sys.stderr)
        return 2
    config_path.write_text(default_config_text(root), encoding="utf-8")
    config = load_config(config_path)
    ensure_dir(config.root)
    ensure_task_dirs(config)
    init_store(config)
    init_repo_if_needed(config)
    print(f"Initialized Alluvium at {root}")
    print(f"Drop folders or bare files into: {config.inbox_path}")
    print(f"Run: alluvium daemon --config {config_path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    return asyncio.run(_run_daemon_foreground(config, config_path))


def cmd_daemon(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    if args.foreground:
        return asyncio.run(_run_daemon_foreground(config, config_path))
    return start_background_daemon(config, config_path)


async def _run_daemon_foreground(config, config_path: Path) -> int:
    daemon = AlluviumDaemon(config, config_path=config_path)
    await daemon.run()
    return 0


def start_background_daemon(config, config_path: Path) -> int:
    ensure_task_dirs(config)
    ensure_dir(config.logs_path)
    ensure_dir(config.daemon_dir)

    existing_pid = read_daemon_pid(config)
    if existing_pid and pid_is_running(existing_pid):
        print(f"Alluvium daemon already running with pid {existing_pid}")
        return 0
    if existing_pid and not pid_is_running(existing_pid):
        config.pid_path.unlink(missing_ok=True)

    log_fh = config.daemon_log_path.open("ab")
    command = [sys.executable, "-m", "alluvium.cli", "serve", "--config", str(config_path)]
    proc = subprocess.Popen(
        command,
        cwd=str(config.root),
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log_fh.close()

    deadline = time.time() + 5
    while time.time() < deadline:
        pid = read_daemon_pid(config)
        if pid == proc.pid and pid_is_running(pid):
            print(f"Started Alluvium daemon pid {pid}")
            print(f"Log: {config.daemon_log_path}")
            return 0
        if proc.poll() is not None:
            print(f"daemon exited early with code {proc.returncode}; see {config.daemon_log_path}", file=sys.stderr)
            return proc.returncode or 1
        time.sleep(0.1)
    print(f"Started Alluvium daemon pid {proc.pid} (pid file not observed yet)")
    print(f"Log: {config.daemon_log_path}")
    return 0


def cmd_stop_daemon(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    pid = read_daemon_pid(config)
    if not pid:
        print("Alluvium daemon is not running (no pid file).")
        return 0
    if not pid_is_running(pid):
        print(f"Alluvium daemon pid {pid} is not running; removing stale pid file.")
        config.pid_path.unlink(missing_ok=True)
        return 0

    print(f"Stopping Alluvium daemon pid {pid}...")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not pid_is_running(pid):
            print("Stopped.")
            return 0
        time.sleep(0.2)

    if args.force:
        print(f"Daemon did not stop within {args.timeout}s; sending SIGKILL.")
        os.kill(pid, signal.SIGKILL)
        deadline = time.time() + 5
        while time.time() < deadline:
            if not pid_is_running(pid):
                print("Killed.")
                return 0
            time.sleep(0.2)
        print("SIGKILL sent, but process still appears to exist.", file=sys.stderr)
        return 1

    print(
        f"Daemon still appears to be running after {args.timeout}s. "
        "It may be waiting for active worker cleanup. Use --force to SIGKILL.",
        file=sys.stderr,
    )
    return 1


def cmd_reload(args: argparse.Namespace) -> int:
    if not hasattr(signal, "SIGHUP"):
        print("Config reload is not supported on this platform (no SIGHUP).", file=sys.stderr)
        return 2
    config = load_config(Path(args.config))
    pid = read_daemon_pid(config)
    if not pid or not pid_is_running(pid):
        print("Alluvium daemon is not running.", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGHUP)
    print(f"Sent reload signal to Alluvium daemon pid {pid}.")
    print(f"Check log: {config.daemon_log_path}")
    return 0


async def cmd_run_once(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    if args.ignore_settle:
        config.safety.inbox_settle_seconds = 0
    daemon = AlluviumDaemon(config, config_path=Path(args.config).expanduser().resolve())
    await daemon.run_once()
    return 0


async def cmd_integrate_once(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    daemon = AlluviumDaemon(config, config_path=Path(args.config).expanduser().resolve())
    count = await daemon.integrate_ready_done_tasks(limit=None)
    print(json.dumps({"integrated_or_checked": count}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    print(json.dumps(status_summary(config), indent=2, sort_keys=True))
    return 0


def _find_task_row(config, task_ref: str) -> dict:
    rows = task_rows(config)
    exact = [row for row in rows if row.get("id") == task_ref]
    if exact:
        return exact[0]
    matches = [row for row in rows if str(row.get("id", "")).startswith(task_ref)]
    if not matches:
        raise ValueError(f"no task matches {task_ref!r}")
    if len(matches) > 1:
        ids = ", ".join(str(row.get("id")) for row in matches[:10])
        raise ValueError(f"task prefix {task_ref!r} is ambiguous: {ids}")
    return matches[0]


def _archive_retry_runtime_files(config, task_dir: Path) -> None:
    agent_dir = task_dir / config.reserved_dir
    system_dir = task_dir / config.system_dir
    if not agent_dir.exists() and not system_dir.exists():
        return
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = system_dir / "attempts" / stamp
    moved = False
    # Worker-facing artifacts that should not leak across retries.
    for rel in ["result.json", "result.md", "needs_human.md"]:
        src = agent_dir / rel
        if src.exists():
            ensure_dir(archive / "agent")
            shutil.move(str(src), str(archive / "agent" / src.name))
            moved = True
    for rel in ["logs/stdout.log", "logs/stderr.log"]:
        src = agent_dir / rel
        if src.exists() and src.stat().st_size:
            ensure_dir(archive / "agent" / "logs")
            shutil.move(str(src), str(archive / "agent" / "logs" / src.name))
            moved = True
    # Harness bookkeeping from the previous attempt.
    for rel in ["process.json", "command.json"]:
        src = system_dir / rel
        if src.exists():
            ensure_dir(archive / "system")
            shutil.move(str(src), str(archive / "system" / src.name))
            moved = True
    if moved:
        (archive / "retry.txt").write_text("Archived before retry.\n", encoding="utf-8")


def cmd_retry(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    init_store(config)
    try:
        row = _find_task_row(config, args.task)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    task_id = str(row["id"])
    task_dir = Path(str(row["task_dir"]))
    if not task_dir.exists():
        print(f"task folder is missing; cannot retry: {task_dir}", file=sys.stderr)
        return 1
    state = str(row["state"])
    retryable = {"failed", "needs_revision", "needs_human", "lost", "done", "worker_done"}
    if state not in retryable and not args.from_any_state:
        print(f"task {task_id} is in state {state!r}; use --from-any-state to force", file=sys.stderr)
        return 2
    _archive_retry_runtime_files(config, task_dir)
    mark_task_state(config, task_id, "queued", task_dir=task_dir)
    print(json.dumps({"task_id": task_id, "previous_state": state, "state": "queued", "task_dir": str(task_dir)}, indent=2))
    return 0


def cmd_example_task(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    ensure_task_dirs(config)
    target = config.inbox_path / args.name
    if target.exists():
        i = 1
        while (config.inbox_path / f"{args.name}-{i}").exists():
            i += 1
        target = config.inbox_path / f"{args.name}-{i}"
    ensure_dir(target)
    (target / "request.md").write_text(
        "# Example task\n\nSummarize this task folder and produce any useful output artifacts.\n",
        encoding="utf-8",
    )
    print(target)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "daemon":
        return cmd_daemon(args)
    if args.command == "stop-daemon":
        return cmd_stop_daemon(args)
    if args.command == "reload":
        return cmd_reload(args)
    if args.command == "run-once":
        return asyncio.run(cmd_run_once(args))
    if args.command == "integrate-once":
        return asyncio.run(cmd_integrate_once(args))
    if args.command == "status":
        return cmd_status(args)
    if args.command == "retry":
        return cmd_retry(args)
    if args.command == "example-task":
        return cmd_example_task(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
