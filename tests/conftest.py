"""Shared test setup.

The triage entry gate (ADR-008) defaults a new issue to `triage: true` whenever
it runs in an *agent* context — i.e. `default_comment_actor()` detects an agent
env marker (CLAUDE_CODE_SESSION_ID / CLAUDECODE / AI_AGENT / CODEX_*). The test
runner itself often executes under such markers, which would silently turn every
`cmd_new` in the existing suite into a hidden triage item.

Neutralize that ambient env for ALL tests so the gate's default is `human`
(→ no triage) and the suite is deterministic regardless of who runs it. Tests
that exercise the gate set the markers (or pass `actor=`) explicitly.

The worktree close gate shells out to the developer's installed `registrar`.
Disable it by default for hermetic tests; gate-specific tests opt back in with
`DOCKET_WORKTREE_CLOSE_GATE=1` and a fake registrar on PATH.
"""

from __future__ import annotations

import pytest

# Every env var default_comment_actor() probes (explicit + process-source).
_ACTOR_MARKERS = (
    "DOCKET_COMMENT_ACTOR",
    "DOCKET_ACTOR",
    "CODEX_THREAD_ID",
    "CODEX_CI",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDECODE",
    "AI_AGENT",
    "CLAUDE_SESSION_ID",
    "CLAUDECODE_SESSION_ID",
)


@pytest.fixture(autouse=True)
def _neutralize_actor_env(monkeypatch):
    for name in _ACTOR_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOCKET_WORKTREE_CLOSE_GATE", "0")
