from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def inventory(task_dir: Path, reserved: str = ".agent") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for root, dirs, files in os.walk(task_dir):
        rel_root = Path(root).relative_to(task_dir)
        if rel_root.parts and rel_root.parts[0] == reserved:
            dirs[:] = []
            continue
        for name in files:
            p = Path(root) / name
            rel = p.relative_to(task_dir)
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                size = 0
            rows.append({"path": str(rel), "size": size})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic built-in Alluvium agent.")
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--prompt-file", required=True)
    args = parser.parse_args(argv)

    task_dir = Path(args.task_dir)
    agent_dir = task_dir / ".agent"
    outputs = agent_dir / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    files = inventory(task_dir)
    (agent_dir / "understanding.md").write_text(
        "# Understanding\n\n"
        "This task was processed by the deterministic built-in Alluvium agent. "
        "It inventories inputs but does not perform LLM reasoning. Configure "
        "[agent].command in config.toml to use a commodity coding agent.\n",
        encoding="utf-8",
    )
    (agent_dir / "plan.md").write_text(
        "# Plan\n\n1. Inventory task inputs.\n2. Write a simple result.\n",
        encoding="utf-8",
    )
    (outputs / "inventory.json").write_text(json.dumps(files, indent=2) + "\n", encoding="utf-8")
    (agent_dir / "result.md").write_text(
        "# Result\n\n"
        f"Inventoried {len(files)} input file(s). No repository changes were made.\n",
        encoding="utf-8",
    )
    (agent_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "summary": f"Inventoried {len(files)} input file(s).",
                "outputs": [".agent/outputs/inventory.json"],
                "repo_changed": False,
                "external_effects": False,
                "needs_human": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
