from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import Config, default_config_text, load_config
from .daemon import AlluviumDaemon, status_summary
from .fsqueue import ensure_task_dirs
from .gitops import init_repo_if_needed
from .util import ensure_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alluvium", description="Free-form task inbox daemon for coding agents.")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize an Alluvium root directory.")
    init.add_argument("root", nargs="?", default=".", help="Root directory to initialize.")
    init.add_argument("--force", action="store_true", help="Overwrite config.toml if it exists.")

    daemon = sub.add_parser("daemon", help="Run the long-running daemon.")
    daemon.add_argument("--config", default="config.toml", help="Path to config.toml.")

    once = sub.add_parser("run-once", help="Claim current inbox tasks, run workers, and integrate once.")
    once.add_argument("--config", default="config.toml")
    once.add_argument("--ignore-settle", action="store_true", help="Process inbox items immediately, ignoring inbox_settle_seconds.")

    integrate = sub.add_parser("integrate-once", help="Integrate all pending done-task branches once.")
    integrate.add_argument("--config", default="config.toml")

    status = sub.add_parser("status", help="Print JSON status summary.")
    status.add_argument("--config", default="config.toml")

    example = sub.add_parser("example-task", help="Drop an example task into tasks/inbox.")
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
    init_repo_if_needed(config)
    print(f"Initialized Alluvium at {root}")
    print(f"Drop folders or bare files into: {config.tasks_path / 'inbox'}")
    print(f"Run: alluvium daemon --config {config_path}")
    return 0


async def cmd_daemon(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    daemon = AlluviumDaemon(config)
    await daemon.run()
    return 0


async def cmd_run_once(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    if args.ignore_settle:
        config.safety.inbox_settle_seconds = 0
    daemon = AlluviumDaemon(config)
    await daemon.run_once()
    return 0


async def cmd_integrate_once(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    daemon = AlluviumDaemon(config)
    count = await daemon.integrate_ready_done_tasks(limit=None)
    print(json.dumps({"integrated_or_checked": count}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    print(json.dumps(status_summary(config), indent=2, sort_keys=True))
    return 0


def cmd_example_task(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    ensure_task_dirs(config)
    target = config.tasks_path / "inbox" / args.name
    if target.exists():
        i = 1
        while (config.tasks_path / "inbox" / f"{args.name}-{i}").exists():
            i += 1
        target = config.tasks_path / "inbox" / f"{args.name}-{i}"
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
    if args.command == "daemon":
        return asyncio.run(cmd_daemon(args))
    if args.command == "run-once":
        return asyncio.run(cmd_run_once(args))
    if args.command == "integrate-once":
        return asyncio.run(cmd_integrate_once(args))
    if args.command == "status":
        return cmd_status(args)
    if args.command == "example-task":
        return cmd_example_task(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
