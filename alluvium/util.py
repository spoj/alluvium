from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

UTC = dt.UTC


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def utc_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def slugify(value: str, *, max_len: int = 64) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    if not value:
        value = "task"
    return value[:max_len].strip("-._") or "task"


def make_task_id(original_name: str) -> str:
    return f"{utc_stamp()}-{slugify(original_name)}-{secrets.token_hex(3)}"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def append_jsonl(path: Path, data: Mapping[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(data), sort_keys=True) + "\n")


def append_event(task_dir: Path, system_dir: str, event: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"ts": iso_now(), "event": event}
    payload.update(fields)
    append_jsonl(task_dir / system_dir / "events.jsonl", payload)


def tree_latest_mtime(path: Path) -> float:
    try:
        latest = path.stat().st_mtime
    except FileNotFoundError:
        return 0.0
    if path.is_dir():
        for root, dirs, files in os.walk(path):
            # Include dirs and files; ignore broken paths that disappear during scan.
            for name in [*dirs, *files]:
                p = Path(root) / name
                try:
                    latest = max(latest, p.stat().st_mtime)
                except FileNotFoundError:
                    continue
    return latest


def path_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copytree_or_file(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        ensure_dir(dst.parent)
        shutil.copy2(src, dst)


class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str] | str, returncode: int, stdout: str, stderr: str):
        self.args_value = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"command failed ({returncode}): {args}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")


def run_cmd(
    args: Sequence[str] | str,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **dict(env or {})},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
    )
    if check and proc.returncode != 0:
        raise CommandError(args, proc.returncode, proc.stdout, proc.stderr)
    return proc


async def arun_cmd(
    args: Sequence[str] | str,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    shell: bool = False,
    timeout: float | None = None,
) -> tuple[int, str, str]:
    full_env = {**os.environ, **dict(env or {})}
    if shell:
        proc = await asyncio.create_subprocess_shell(
            args if isinstance(args, str) else " ".join(args),
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        assert not isinstance(args, str), "non-shell command must be a sequence"
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        out_b, err_b = await proc.communicate()
        out = out_b.decode(errors="replace")
        err = err_b.decode(errors="replace") + "\n[TIMEOUT]"
        if check:
            raise CommandError(args, -9, out, err)
        return -9, out, err
    out = out_b.decode(errors="replace")
    err = err_b.decode(errors="replace")
    if check and proc.returncode != 0:
        raise CommandError(args, proc.returncode or 1, out, err)
    return proc.returncode or 0, out, err


def relative_to_or_abs(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def render_command(parts: Iterable[str], mapping: Mapping[str, str]) -> list[str]:
    rendered = []
    for part in parts:
        try:
            rendered.append(part.format(**mapping))
        except KeyError:
            rendered.append(part)
    return rendered
