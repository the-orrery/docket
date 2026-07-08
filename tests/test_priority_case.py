"""Tests for case-insensitive priority resolution.

resolve_priority must map any casing of a canonical priority name, plus common
"No priority" aliases, to the canonical display form — and reject truly unknown
values with a clear error message.
"""

from __future__ import annotations

import pytest

from docket import commands as C
from docket.errors import DocketError
from docket.issue import load_by_id
from docket.states import resolve_priority

# ---- unit: resolve_priority ----


@pytest.mark.parametrize(
    "inp, want",
    [
        # exact canonical forms (fast path)
        ("Urgent", "Urgent"),
        ("High", "High"),
        ("Medium", "Medium"),
        ("Low", "Low"),
        ("No priority", "No priority"),
        # lowercase
        ("urgent", "Urgent"),
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
        # uppercase
        ("URGENT", "Urgent"),
        ("HIGH", "High"),
        ("MEDIUM", "Medium"),
        ("LOW", "Low"),
        # mixed case
        ("hIgH", "High"),
        ("uRgEnT", "Urgent"),
        # "No priority" aliases
        ("none", "No priority"),
        ("None", "No priority"),
        ("NONE", "No priority"),
        ("no-priority", "No priority"),
        ("NO-PRIORITY", "No priority"),
        ("no priority", "No priority"),
        ("NO PRIORITY", "No priority"),
    ],
)
def test_resolve_priority_variants(inp, want):
    assert resolve_priority(inp) == want


def test_resolve_priority_rejects_unknown():
    with pytest.raises(DocketError, match="invalid priority"):
        resolve_priority("critical")


def test_resolve_priority_error_lists_valid_values():
    with pytest.raises(DocketError, match="Urgent"):
        resolve_priority("bogus")


# ---- integration: cmd_new accepts mixed-case --priority ----


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


def test_new_priority_lowercase(repo):
    C.cmd_new("x", "", "high", None, "", "", "")
    assert load_by_id("ISSUE-1").priority() == "High"


def test_new_priority_uppercase(repo):
    C.cmd_new("x", "", "URGENT", None, "", "", "")
    assert load_by_id("ISSUE-1").priority() == "Urgent"


def test_new_priority_mixed_case(repo):
    C.cmd_new("x", "", "hIgH", None, "", "", "")
    assert load_by_id("ISSUE-1").priority() == "High"


def test_new_priority_none_alias(repo):
    C.cmd_new("x", "", "none", None, "", "", "")
    assert load_by_id("ISSUE-1").priority() == "No priority"


def test_new_priority_invalid_still_errors(repo):
    with pytest.raises(DocketError, match="invalid priority"):
        C.cmd_new("x", "", "critical", None, "", "", "")


# ---- integration: cmd_set accepts mixed-case --priority ----


def _make(repo, title="x", priority="No priority"):
    C.cmd_new(title, "", priority, None, "", "", "")


def test_set_priority_lowercase(repo):
    _make(repo)
    C.cmd_set("ISSUE-1", priority="low")
    assert load_by_id("ISSUE-1").priority() == "Low"


def test_set_priority_uppercase(repo):
    _make(repo)
    C.cmd_set("ISSUE-1", priority="HIGH")
    assert load_by_id("ISSUE-1").priority() == "High"


def test_set_priority_mixed_case(repo):
    _make(repo)
    C.cmd_set("ISSUE-1", priority="mEdIuM")
    assert load_by_id("ISSUE-1").priority() == "Medium"


def test_set_priority_none_alias(repo):
    _make(repo, priority="High")
    C.cmd_set("ISSUE-1", priority="no-priority")
    assert load_by_id("ISSUE-1").priority() == "No priority"


def test_set_priority_invalid_still_errors(repo):
    _make(repo)
    with pytest.raises(DocketError, match="invalid priority"):
        C.cmd_set("ISSUE-1", priority="bogus")
