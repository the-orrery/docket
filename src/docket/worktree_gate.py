"""Registrar-backed worktree close gate for issue completion."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any

from .errors import DocketError
from .issue import Issue
from .projects import display_id, load_projects

_CLOSING_STATES = {"completed", "canceled"}
_DISABLE_VALUES = {"0", "false", "no", "off"}


def is_closing_state(state_type: str) -> bool:
    return state_type in _CLOSING_STATES


def ensure_worktrees_reconciled(is_: Issue, state_type: str) -> None:
    if not is_closing_state(state_type):
        return
    if os.environ.get("DOCKET_WORKTREE_CLOSE_GATE", "1").lower() in _DISABLE_VALUES:
        return
    registrar = shutil.which("registrar")
    if registrar is None:
        return

    refs = _owner_refs(is_)
    cmd = [registrar, "worktree", "reconcile", refs[0]]
    for alias in refs[1:]:
        cmd.extend(["--alias", alias])
    cmd.extend(["--format", "json"])
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocketError(f"worktree close gate failed: {exc}") from exc

    if result.returncode == 0:
        return

    payload = _json_payload(result.stdout)
    if not payload:
        if _registrar_reconcile_missing(result.stderr):
            return
        message = (result.stderr or result.stdout).strip() or "registrar failed"
        raise DocketError(f"worktree close gate failed: {message}")
    if payload.get("blocked"):
        raise DocketError(_blocked_message(is_, payload))


def _owner_refs(is_: Issue) -> list[str]:
    refs = [is_.id()]
    projects, _problems = load_projects()
    display = display_id(is_, projects)
    if display and display not in refs:
        refs.append(display)
    return refs


def _json_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _registrar_reconcile_missing(stderr: str) -> bool:
    text = stderr.lower()
    return "no such command" in text and "reconcile" in text


def _blocked_message(is_: Issue, payload: dict[str, Any]) -> str:
    active_count = payload.get("active_count", 0)
    lines = [
        f"worktree close gate blocked {is_.id()}: "
        f"{active_count} active worktree(s) still attached",
        "merge or delete every attached worktree before closing this issue:",
    ]
    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "-")
        state = str(item.get("close_gate_state") or "-")
        action = str(item.get("close_gate_action") or "")
        lines.append(f"- {name}: {state}; {action}")
    lines.append(
        "override only for emergencies: DOCKET_WORKTREE_CLOSE_GATE=0 docket finish "
        f"{is_.id()}"
    )
    return "\n".join(lines)


def warn_if_gate_unavailable() -> None:
    if shutil.which("registrar") is None:
        print("worktree close gate skipped: registrar not found", file=sys.stderr)
