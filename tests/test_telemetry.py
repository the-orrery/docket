"""Telemetry tests for the docket glue layer.

Tests cover the public API that docket modules use: record, stats, db_path,
connect, Tee, STDOUT_CAP/STDERR_CAP. Private internals (_pctile, _is_fault,
_connect, run_instrumented) lived in the old vendored copy and are not part
of this glue layer — behavioral coverage comes through record/stats round-trips.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from docket import telemetry


def _last_meta(db: Path) -> dict:
    """Return the parsed meta JSON of the most recently inserted row."""
    conn = telemetry.connect(db)
    try:
        raw = conn.execute(
            "SELECT meta FROM calls ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw)


def _rec(verb: str, exit_code: int = 0, duration_ms: int = 10, err: str = "") -> dict:
    return {
        "ts": "2026-01-01T00:00:00.000+00:00",
        "pid": 1,
        "command_path": [verb],
        "args": [],
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "err": err,
    }


def test_record_and_stats_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    for i in range(5):
        telemetry.record(_rec("list", duration_ms=i * 10), path=db)
    telemetry.record(_rec("new", exit_code=2, err="boom"), path=db)
    out = telemetry.stats(path=db)
    assert "list" in out
    assert "new" in out
    assert "6 calls" in out
    assert "boom" in out  # the fault message surfaces in recent errors


def test_stats_missing_db(tmp_path: Path) -> None:
    assert "no telemetry yet" in telemetry.stats(path=tmp_path / "absent.db")


def test_stats_error_rate_behavioral(tmp_path: Path) -> None:
    """Verify that exit>=2 or non-empty err counts as a fault in stats output.

    This is a behavioral replacement for the old _is_fault unit test:
    the fault-classification logic now lives in the telemetry implementation.
    """
    db = tmp_path / "e.db"
    # Not a fault: intentional exit-1 without a message (lint found problems)
    telemetry.record(_rec("lint", exit_code=1, err=""), path=db)
    # Real faults: non-empty err, or hard exit >= 2
    telemetry.record(_rec("cmd", exit_code=2, err=""), path=db)
    telemetry.record(_rec("cmd", exit_code=0, err="oops"), path=db)
    out = telemetry.stats(path=db)
    # 2 faults out of 3 calls
    assert "2 errors" in out


def test_disabled_suppresses_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "off.db"
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    telemetry.record(_rec("list"), path=db)
    assert not db.exists()  # nothing written when opted out


def test_record_best_effort_never_raises(tmp_path: Path) -> None:
    bad_parent = tmp_path / "afile"
    bad_parent.write_text("x")  # a file → can't mkdir a dir under it
    telemetry.record(_rec("x"), path=bad_parent / "sub.db")  # must not raise
    assert not (bad_parent / "sub.db").exists()


def test_tee_accounting_never_raises() -> None:
    class _Sink:
        def __init__(self) -> None:
            self.parts: list = []

        def write(self, s: object) -> int:
            self.parts.append(s)
            return 1

    t = telemetry.Tee(_Sink(), cap=100)
    t.write("hello")
    t.write(b"bytes")  # non-str input must not raise in the accounting path
    assert "hello" in t.sample


def test_tee_caps_are_correct() -> None:
    assert telemetry.STDOUT_CAP == 2048
    assert telemetry.STDERR_CAP == 4096


def test_stats_corrupt_db(tmp_path: Path) -> None:
    db = tmp_path / "bad.db"
    db.write_text("not a database")
    assert "unreadable" in telemetry.stats(path=db)  # human note, never a traceback


def test_concurrent_writes(tmp_path: Path) -> None:
    """Many processes appending at once must not lose or corrupt rows — the whole
    reason for SQLite (WAL + busy_timeout) over a flat jsonl."""
    if not hasattr(os, "fork"):
        pytest.skip("needs fork for true multi-process concurrency")
    import multiprocessing as mp

    db = tmp_path / "c.db"
    telemetry.record(_rec("seed"), path=db)  # create schema once before the race

    per_proc, n_proc = 25, 8

    def worker() -> None:  # fork inherits this closure — no pickling needed
        for _ in range(per_proc):
            telemetry.record(_rec("hit"), path=db)

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=worker) for _ in range(n_proc)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        assert p.exitcode == 0

    conn = telemetry.connect(db)
    try:
        total = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
        hits = conn.execute(
            "SELECT count(*) FROM calls WHERE command_path='[\"hit\"]'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert hits == per_proc * n_proc  # no lost writes under contention
    assert total == per_proc * n_proc + 1
