"""Local per-invocation telemetry for docket.

The ledger is a best-effort SQLite file on the user's machine. It is used only
for local command diagnostics: telemetry errors must never change a command's
exit code or write to the network.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from docket import __version__

STDOUT_CAP = 2048
STDERR_CAP = 4096

_ENV_DB = "DOCKET_TELEMETRY_DB"
_ENV_OFF = "DOCKET_TELEMETRY_OFF"
_HARD_EXIT_MIN = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL DEFAULT '',
    pid           INTEGER NOT NULL DEFAULT 0,
    command_path  TEXT    NOT NULL DEFAULT '[]',
    args          TEXT    NOT NULL DEFAULT '[]',
    exit_code     INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    out_bytes     INTEGER NOT NULL DEFAULT 0,
    stdout        TEXT    NOT NULL DEFAULT '',
    stderr        TEXT    NOT NULL DEFAULT '',
    err           TEXT    NOT NULL DEFAULT '',
    cwd           TEXT    NOT NULL DEFAULT '',
    version       TEXT    NOT NULL DEFAULT '',
    is_tty        INTEGER NOT NULL DEFAULT 0,
    is_ci         INTEGER NOT NULL DEFAULT 0,
    meta          TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS calls_command_path ON calls(command_path);
"""

_COLUMNS = (
    "ts",
    "pid",
    "command_path",
    "args",
    "exit_code",
    "duration_ms",
    "out_bytes",
    "stdout",
    "stderr",
    "err",
    "cwd",
    "version",
    "is_tty",
    "is_ci",
    "meta",
)


class Tee:
    """Pass-through stream wrapper that counts bytes and keeps a small sample."""

    def __init__(self, real: Any, cap: int) -> None:
        self.real = real
        self.cap = cap
        self.total = 0
        self._buf = bytearray()

    def write(self, s: str) -> int:
        n = self.real.write(s)
        try:
            b = s.encode("utf-8", "replace") if isinstance(s, str) else bytes(s)
            self.total += len(b)
            if len(self._buf) < self.cap:
                self._buf.extend(b[: self.cap - len(self._buf)])
        except Exception:
            pass
        return n if isinstance(n, int) else len(s)

    def flush(self) -> None:
        self.real.flush()

    def isatty(self) -> bool:
        try:
            return self.real.isatty()
        except Exception:
            return False

    @property
    def sample(self) -> str:
        return self._buf.decode("utf-8", "replace")


def db_path() -> Path:
    override = os.environ.get(_ENV_DB)
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "docket" / "telemetry.db"


def _disabled() -> bool:
    return bool(os.environ.get(_ENV_OFF) or os.environ.get("DO_NOT_TRACK"))


def connect(path: Path) -> sqlite3.Connection:
    """Open the telemetry ledger and ensure the current schema exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version=2")
    except Exception:
        conn.close()
        raise
    return conn


def record(rec: dict, *, path: Path | None = None) -> None:
    """Insert one invocation row; failures are intentionally swallowed."""
    if _disabled():
        return
    try:
        conn = connect(path or db_path())
        try:
            command_path = rec.get("command_path", [])
            values = (
                str(rec.get("ts", "")),
                int(rec.get("pid", 0)),
                json.dumps(command_path, ensure_ascii=False),
                json.dumps(rec.get("args", []), ensure_ascii=False),
                int(rec.get("exit_code", 0)),
                int(rec.get("duration_ms", 0)),
                int(rec.get("out_bytes", 0)),
                str(rec.get("stdout", "")),
                str(rec.get("stderr", "")),
                str(rec.get("err", "")),
                str(rec.get("cwd", "")),
                str(rec.get("version", __version__)),
                1 if rec.get("is_tty") else 0,
                1 if rec.get("is_ci") else 0,
                json.dumps(rec.get("meta", {}), ensure_ascii=False),
            )
            placeholders = ",".join("?" * len(_COLUMNS))
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT INTO calls ({','.join(_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
    except Exception:
        return


def _pctile(xs: list[int], p: int) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    if p >= 100:
        return s[-1]
    idx = p * len(s) // 100
    return s[min(idx, len(s) - 1)]


def _is_fault(exit_code: int, err: str) -> bool:
    return bool(err.strip()) or exit_code >= _HARD_EXIT_MIN


def _first_line(s: str) -> str:
    s = s.strip()
    i = s.find("\n")
    return s[:i] if i >= 0 else s


def _verb(command_path: str) -> str:
    try:
        parsed = json.loads(command_path)
    except json.JSONDecodeError:
        return command_path
    if isinstance(parsed, list) and parsed:
        return str(parsed[0])
    return ""


def stats(path: Path | None = None) -> str:
    """Per-verb summary plus recent faults, as human-readable text."""
    p = path or db_path()
    if not p.exists():
        return f"no telemetry yet ({p}): run the CLI a few times first"
    try:
        conn = connect(p)
        try:
            rows = conn.execute(
                "SELECT command_path, exit_code, duration_ms, err "
                "FROM calls WHERE command_path != '[]' ORDER BY id"
            ).fetchall()
            recent_rows = conn.execute(
                "SELECT ts, command_path, err, stderr FROM calls "
                "WHERE command_path != '[]' AND (err != '' OR exit_code >= 2) "
                "ORDER BY id DESC LIMIT 10"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return f"telemetry ledger unreadable ({p}); remove it to reset"

    if not rows:
        return "telemetry is empty"

    count: dict[str, int] = {}
    durs: dict[str, list[int]] = {}
    err_cnt: dict[str, int] = {}
    total = total_err = 0

    for command_path, exit_code, duration_ms, err in rows:
        verb = _verb(command_path)
        if not verb:
            continue
        total += 1
        count[verb] = count.get(verb, 0) + 1
        durs.setdefault(verb, []).append(int(duration_ms or 0))
        if _is_fault(int(exit_code or 0), err or ""):
            total_err += 1
            err_cnt[verb] = err_cnt.get(verb, 0) + 1

    if not total:
        return "telemetry is empty"

    verbs = sorted(count, key=lambda v: (-count[v], v))
    rate = 100.0 * total_err / total
    lines = [f"docket telemetry — {total} calls · {total_err} errors ({rate:.1f}%)", ""]
    lines.append(f"{'verb':<14}{'count':>7}{'p50':>8}{'p95':>8}{'max':>8}{'err':>6}")
    for v in verbs:
        d = durs[v]
        lines.append(
            f"{v:<14}{count[v]:>7}"
            f"{str(_pctile(d, 50)) + 'ms':>8}"
            f"{str(_pctile(d, 95)) + 'ms':>8}"
            f"{str(_pctile(d, 100)) + 'ms':>8}"
            f"{err_cnt.get(v, 0):>6}"
        )
    if recent_rows:
        lines.append("")
        lines.append("recent errors (last 10):")
        for ts, command_path, err, stderr in reversed(recent_rows):
            msg = (err or "").strip() or _first_line(stderr or "")
            lines.append(f"  {ts}  {_verb(command_path):<12} {msg}")
    return "\n".join(lines)


__all__ = [
    "STDERR_CAP",
    "STDOUT_CAP",
    "Tee",
    "connect",
    "db_path",
    "record",
    "stats",
]
