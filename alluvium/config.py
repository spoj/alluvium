from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AgentConfig:
    command: list[str] = field(
        default_factory=lambda: [
            "pi",
            "--model",
            "gpt5/gpt-5.4:high",
            "--no-session",
            "-p",
            "@{prompt_file}",
        ]
    )
    timeout_seconds: int = 3600


@dataclass(slots=True)
class IntegrationConfig:
    enabled: bool = True
    base_branch: str = "main"
    strategy: str = "squash_merge"
    run_tests: list[str] = field(default_factory=list)
    return_blocked_for_revision: bool = True
    max_revision_rounds: int = 2
    move_unrevisionable_to_needs_human: bool = True
    cleanup_after_merge: bool = True


@dataclass(slots=True)
class GitConfig:
    author_name: str = "Alluvium"
    author_email: str = "alluvium@example.local"
    auto_commit_worker_changes: bool = True


@dataclass(slots=True)
class SafetyConfig:
    inbox_settle_seconds: int = 5
    max_workers: int = 2
    scan_interval_seconds: int = 5
    integrator_interval_seconds: int = 10
    janitor_interval_seconds: int = 60
    shutdown_grace_seconds: int = 10
    ignore_name_prefixes: list[str] = field(default_factory=lambda: ["."])
    ignore_name_suffixes: list[str] = field(default_factory=lambda: [".tmp", ".part", ".partial", ".crdownload"])


@dataclass(slots=True)
class Config:
    root: Path
    repo_path: Path
    inbox_path: Path
    tasks_path: Path
    worktrees_path: Path
    logs_path: Path
    reserved_dir: str = ".agent"
    agent: AgentConfig = field(default_factory=AgentConfig)
    integration: IntegrationConfig = field(default_factory=IntegrationConfig)
    git: GitConfig = field(default_factory=GitConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    @property
    def daemon_dir(self) -> Path:
        return self.root / ".alluvium"

    @property
    def lock_path(self) -> Path:
        return self.daemon_dir / "daemon.lock"

    @property
    def pid_path(self) -> Path:
        return self.daemon_dir / "daemon.pid"

    @property
    def daemon_log_path(self) -> Path:
        return self.logs_path / "daemon.log"


def _get_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config section [{name}] must be a table")
    return value


def _path(value: str | Path, base: Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    root = _path(raw.get("root", "."), base)
    repo_path = _path(raw.get("repo_path", "repo"), root)
    inbox_path = _path(raw.get("inbox_path", "inbox"), root)
    tasks_path = _path(raw.get("tasks_path", "tasks"), root)
    worktrees_path = _path(raw.get("worktrees_path", "worktrees"), root)
    logs_path = _path(raw.get("logs_path", "logs"), root)

    agent_raw = _get_section(raw, "agent")
    integration_raw = _get_section(raw, "integration")
    git_raw = _get_section(raw, "git")
    safety_raw = _get_section(raw, "safety")

    agent = AgentConfig(
        command=list(agent_raw.get("command", AgentConfig().command)),
        timeout_seconds=int(agent_raw.get("timeout_seconds", 3600)),
    )
    integration = IntegrationConfig(
        enabled=bool(integration_raw.get("enabled", True)),
        base_branch=str(integration_raw.get("base_branch", "main")),
        strategy=str(integration_raw.get("strategy", "squash_merge")),
        run_tests=list(integration_raw.get("run_tests", [])),
        return_blocked_for_revision=bool(integration_raw.get("return_blocked_for_revision", True)),
        max_revision_rounds=int(integration_raw.get("max_revision_rounds", 2)),
        move_unrevisionable_to_needs_human=bool(
            integration_raw.get(
                "move_unrevisionable_to_needs_human",
                integration_raw.get("move_blocked_to_needs_human", True),
            )
        ),
        cleanup_after_merge=bool(integration_raw.get("cleanup_after_merge", True)),
    )
    git = GitConfig(
        author_name=str(git_raw.get("author_name", "Alluvium")),
        author_email=str(git_raw.get("author_email", "alluvium@example.local")),
        auto_commit_worker_changes=bool(git_raw.get("auto_commit_worker_changes", True)),
    )
    safety = SafetyConfig(
        inbox_settle_seconds=int(safety_raw.get("inbox_settle_seconds", 5)),
        max_workers=int(safety_raw.get("max_workers", 2)),
        scan_interval_seconds=int(safety_raw.get("scan_interval_seconds", 5)),
        integrator_interval_seconds=int(safety_raw.get("integrator_interval_seconds", 10)),
        janitor_interval_seconds=int(safety_raw.get("janitor_interval_seconds", 60)),
        shutdown_grace_seconds=int(safety_raw.get("shutdown_grace_seconds", 10)),
        ignore_name_prefixes=list(safety_raw.get("ignore_name_prefixes", ["."])),
        ignore_name_suffixes=list(safety_raw.get("ignore_name_suffixes", [".tmp", ".part", ".partial", ".crdownload"])),
    )

    return Config(
        root=root,
        repo_path=repo_path,
        inbox_path=inbox_path,
        tasks_path=tasks_path,
        worktrees_path=worktrees_path,
        logs_path=logs_path,
        reserved_dir=str(raw.get("reserved_dir", ".agent")),
        agent=agent,
        integration=integration,
        git=git,
        safety=safety,
    )


def default_config_text(root: Path) -> str:
    root = root.resolve()
    root_toml = json.dumps(str(root))
    return f'''# Alluvium configuration.
# Run with: alluvium serve --config {root / "config.toml"}

root = {root_toml}
repo_path = "repo"
inbox_path = "inbox"
tasks_path = "tasks"
worktrees_path = "worktrees"
logs_path = "logs"
reserved_dir = ".agent"

[agent]
# Default uses pi. Placeholders: {{task_id}}, {{task_dir}}, {{agent_dir}}, {{worktree}}, {{branch}}, {{prompt_file}}
# For a deterministic smoke-test worker, use:
# command = ["{sys.executable}", "-m", "alluvium.builtin_agent", "--task-dir", "{{task_dir}}", "--worktree", "{{worktree}}", "--prompt-file", "{{prompt_file}}"]
command = ["pi", "--model", "gpt5/gpt-5.4:high", "--no-session", "-p", "@{{prompt_file}}"]
timeout_seconds = 3600

[integration]
enabled = true
base_branch = "main"
strategy = "squash_merge"
run_tests = []
return_blocked_for_revision = true
max_revision_rounds = 2
move_unrevisionable_to_needs_human = true
cleanup_after_merge = true

[git]
author_name = "Alluvium"
author_email = "alluvium@example.local"
auto_commit_worker_changes = true

[safety]
max_workers = 2
inbox_settle_seconds = 5
scan_interval_seconds = 5
integrator_interval_seconds = 10
janitor_interval_seconds = 60
shutdown_grace_seconds = 10
ignore_name_prefixes = ["."]
ignore_name_suffixes = [".tmp", ".part", ".partial", ".crdownload"]
'''
