"""The canonical id prefix is configurable via $DOCKET_ID_PREFIX.

The build default stays the repo's existing neutral "ISSUE"; a re-sourced
deployment sets the env to keep minting/normalizing against its own prefix
(e.g. "TEAM"). The read path must still accept any display prefix (project
labels like CORE-1), which resolve to the same on-disk canonical id."""

from __future__ import annotations

import pytest

from docket import commands as C
from docket.issue import id_num, id_prefix, load_by_id, normalize_id


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    return tmp_path


def _make(title="x"):
    C.cmd_new(title, "", "No priority", None, "", "", "", status=None)


# ---- default (no env) keeps the existing ISSUE prefix ----


def test_default_prefix_is_issue(monkeypatch):
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    assert id_prefix() == "ISSUE"


def test_new_mints_issue_by_default(repo, monkeypatch):
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    _make()
    is_ = load_by_id("ISSUE-1")
    assert is_.id() == "ISSUE-1"


def test_blank_env_falls_back_to_default(repo, monkeypatch):
    monkeypatch.setenv("DOCKET_ID_PREFIX", "   ")
    assert id_prefix() == "ISSUE"
    _make()
    assert load_by_id("ISSUE-1").id() == "ISSUE-1"


# ---- env override ----


def test_env_override_changes_prefix(monkeypatch):
    monkeypatch.setenv("DOCKET_ID_PREFIX", "TEAM")
    assert id_prefix() == "TEAM"


def test_new_mints_configured_prefix(repo, monkeypatch):
    monkeypatch.setenv("DOCKET_ID_PREFIX", "TEAM")
    _make()
    is_ = load_by_id("TEAM-1")
    assert is_.id() == "TEAM-1"
    # the file on disk carries the configured canonical prefix
    assert (repo / "issues" / "TEAM-1.md").exists()


# ---- read path accepts any display prefix (unchanged) ----


def test_normalize_maps_to_configured_canonical(monkeypatch):
    monkeypatch.setenv("DOCKET_ID_PREFIX", "TEAM")
    # bare number, default-prefix in any case, and any project prefix all map to
    # the configured canonical id (the number is the true anchor)
    assert normalize_id("1") == "TEAM-1"
    assert normalize_id("issue-1") == "TEAM-1"
    assert normalize_id("CORE-1") == "TEAM-1"
    assert normalize_id("WEB-286") == "TEAM-286"


def test_foreign_display_id_reads_the_underlying_issue(repo, monkeypatch):
    monkeypatch.setenv("DOCKET_ID_PREFIX", "TEAM")
    _make("hello")
    # CORE-1 is a display alias; reading it resolves to the same canonical issue
    assert load_by_id("CORE-1").title() == "hello"
    assert load_by_id("TEAM-1").title() == "hello"


def test_id_num_tracks_configured_prefix(monkeypatch):
    monkeypatch.setenv("DOCKET_ID_PREFIX", "TEAM")
    assert id_num("TEAM-7") == (7, True)
    # a foreign/display prefix is not the canonical id -> not counted
    assert id_num("CORE-7") == (0, False)
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    assert id_num("ISSUE-7") == (7, True)
    assert id_num("TEAM-7") == (0, False)
