from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import Config
from .util import ensure_dir, iso_now, read_json


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  original_name TEXT,
  task_dir TEXT NOT NULL,
  branch TEXT,
  base_commit TEXT,
  head_commit TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  event TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id);
"""


def db_path(config: Config) -> Path:
    return config.daemon_dir / "alluvium.db"


def connect(config: Config) -> sqlite3.Connection:
    ensure_dir(config.daemon_dir)
    conn = sqlite3.connect(db_path(config))
    conn.row_factory = sqlite3.Row
    return conn


def init_store(config: Config) -> None:
    with connect(config) as conn:
        conn.executescript(SCHEMA)


def _task_identity(config: Config, task_dir: Path) -> dict[str, Any]:
    data = read_json(task_dir / config.system_dir / "identity.json", {})
    return data if isinstance(data, dict) else {}


def _repo_metadata(config: Config, task_dir: Path) -> dict[str, str | None]:
    repo_dir = task_dir / config.system_dir / "repo"

    def read(name: str) -> str | None:
        p = repo_dir / name
        try:
            return p.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    return {
        "branch": read("branch.txt"),
        "base_commit": read("base_commit.txt"),
        "head_commit": read("head_commit.txt"),
    }


def upsert_task(config: Config, task_dir: Path, state: str) -> None:
    """Mirror task state to SQLite for robust queries/recovery.

    The filesystem remains the human-readable artifact store. SQLite is the
    compact control-plane index: state, current path, branch metadata, and event
    history.
    """
    init_store(config)
    now = iso_now()
    ident = _task_identity(config, task_dir)
    repo = _repo_metadata(config, task_dir)
    task_id = str(ident.get("task_id") or task_dir.name)
    original_name = ident.get("original_name")
    with connect(config) as conn:
        conn.execute(
            """
            INSERT INTO tasks(id, state, original_name, task_dir, branch, base_commit, head_commit, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              state=excluded.state,
              original_name=COALESCE(excluded.original_name, tasks.original_name),
              task_dir=excluded.task_dir,
              branch=COALESCE(excluded.branch, tasks.branch),
              base_commit=COALESCE(excluded.base_commit, tasks.base_commit),
              head_commit=COALESCE(excluded.head_commit, tasks.head_commit),
              updated_at=excluded.updated_at
            """,
            (
                task_id,
                state,
                str(original_name) if original_name is not None else None,
                str(task_dir),
                repo["branch"],
                repo["base_commit"],
                repo["head_commit"],
                now,
                now,
            ),
        )


def record_event(config: Config, task_id: str, event: str, **payload: Any) -> None:
    init_store(config)
    with connect(config) as conn:
        conn.execute(
            "INSERT INTO events(task_id, ts, event, payload_json) VALUES (?, ?, ?, ?)",
            (task_id, iso_now(), event, json.dumps(payload, sort_keys=True)),
        )


def set_task_state(config: Config, task_dir: Path, state: str) -> None:
    upsert_task(config, task_dir, state)


def mark_task_state(config: Config, task_id: str, state: str, *, task_dir: Path | None = None) -> None:
    init_store(config)
    now = iso_now()
    with connect(config) as conn:
        if task_dir is None:
            conn.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?", (state, now, task_id))
        else:
            conn.execute(
                "UPDATE tasks SET state = ?, task_dir = ?, updated_at = ? WHERE id = ?",
                (state, str(task_dir), now, task_id),
            )


def task_rows(config: Config) -> list[dict[str, Any]]:
    init_store(config)
    with connect(config) as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
    return [dict(row) for row in rows]


def task_row(config: Config, task_id: str) -> dict[str, Any] | None:
    init_store(config)
    with connect(config) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def tasks_by_state(config: Config, state: str) -> list[Path]:
    init_store(config)
    with connect(config) as conn:
        rows = conn.execute("SELECT task_dir FROM tasks WHERE state = ? ORDER BY updated_at, id", (state,)).fetchall()
    return [Path(str(row["task_dir"])) for row in rows]


def task_counts(config: Config) -> dict[str, int]:
    init_store(config)
    with connect(config) as conn:
        rows = conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()
    return {str(row["state"]): int(row["n"]) for row in rows}
