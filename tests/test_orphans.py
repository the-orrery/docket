"""Tests for `docket orphans` — the『提交了没关单』detector: cross-check a code
repo's recent commit messages against docket's OPEN issues.

Two repos are in play and deliberately decoupled: the docket DATA repo (a
throwaway DOCKET_ROOT, no git → auto_commit is a no-op) supplies the open-issue
set, while a separate throwaway CODE repo (real git, empty commits carrying the
references) is what gets scanned via --repo."""

from __future__ import annotations

import json
import subprocess

import pytest

from docket import commands as C
from docket.issue import load_by_id

# ---- fixtures ----


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """The docket DATA repo (open-issue source)."""
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    # hermetic: default id prefix is ISSUE-<n> regardless of the dev's env.
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def coderepo(tmp_path):
    """A real git repo whose commit messages get scanned."""
    r = tmp_path / "code"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.test")
    _git(r, "config", "user.name", "tester")
    _git(r, "commit", "--allow-empty", "-q", "-m", "root")
    return r


def _make(title="x", **kw):
    C.cmd_new(
        title,
        kw.get("project", ""),
        kw.get("priority", "No priority"),
        None,
        "",
        kw.get("parent", ""),
        "",
        status=kw.get("status"),
    )


def _commit(coderepo, msg):
    _git(coderepo, "commit", "--allow-empty", "-q", "-m", msg)


def _json(coderepo, **kw):
    C.cmd_orphans(repo=str(coderepo), as_json=True, **kw)


# ---- ref extraction (regex) — false-positive guard ----


def test_ref_regex_ignores_non_issue_tokens():
    rx = C._issue_ref_re({"ISSUE", "DEMO"})
    text = "bump UTF-8, SHA-256, fix per ADR-008 (DEMO-643) and ISSUE-12"
    hits = {(m.group(1).upper(), m.group(2)) for m in rx.finditer(text)}
    # UTF-8 / SHA-256 / ADR-008 are NOT known prefixes → ignored; only real refs.
    assert hits == {("DEMO", "643"), ("ISSUE", "12")}


def test_ref_regex_word_boundary():
    rx = C._issue_ref_re({"ISSUE"})
    # substring inside a longer word must not match
    assert [m.group(2) for m in rx.finditer("MYISSUE-9 xISSUE-9")] == []
    assert [m.group(2) for m in rx.finditer("(ISSUE-9)")] == ["9"]


# ---- core: committed-but-open ----


def test_open_issue_referenced_is_orphan(repo, coderepo, capsys):
    _make("still open")  # ISSUE-1, Todo
    _commit(coderepo, "feat: do the thing (ISSUE-1)")
    capsys.readouterr()
    _json(coderepo)
    data = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in data] == ["ISSUE-1"]
    assert data[0]["status"] == "Todo"
    assert data[0]["n_commits"] == 1
    assert data[0]["commits"][0]["subject"] == "feat: do the thing (ISSUE-1)"


def test_future_wake_issue_referenced_is_orphan(repo, coderepo, capsys):
    _make("sleepy but committed")  # ISSUE-1, Todo
    C.cmd_set("ISSUE-1", wake="2999-01-01")
    _commit(coderepo, "feat: do the sleepy thing (ISSUE-1)")
    capsys.readouterr()
    _json(coderepo)
    data = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in data] == ["ISSUE-1"]


def test_closed_issue_referenced_is_not_orphan(repo, coderepo, capsys):
    _make("finished")  # ISSUE-1
    C.cmd_set("ISSUE-1", status="Done")
    _commit(coderepo, "feat: finished it (ISSUE-1)")
    capsys.readouterr()
    _json(coderepo)
    assert json.loads(capsys.readouterr().out) == []  # closed → happy path


def test_canceled_issue_referenced_is_not_orphan(repo, coderepo, capsys):
    _make("dropped")  # ISSUE-1
    C.cmd_set("ISSUE-1", status="canceled")
    _commit(coderepo, "chore: touched ISSUE-1 then dropped")
    capsys.readouterr()
    _json(coderepo)
    assert json.loads(capsys.readouterr().out) == []


def test_reference_to_unknown_issue_is_skipped(repo, coderepo, capsys):
    _make("real")  # ISSUE-1
    _commit(coderepo, "wip: typo ref ISSUE-999")  # no such issue
    capsys.readouterr()
    _json(coderepo)
    assert json.loads(capsys.readouterr().out) == []


def test_triage_proposal_is_not_orphan(repo, coderepo, capsys, monkeypatch):
    monkeypatch.setenv("AI_AGENT", "1")  # agent context → new issue enters triage gate
    _make("proposed")  # ISSUE-1, triage:true
    monkeypatch.delenv("AI_AGENT", raising=False)
    assert load_by_id("ISSUE-1").is_triage()
    _commit(coderepo, "spike: prototype (ISSUE-1)")
    capsys.readouterr()
    _json(coderepo)
    assert json.loads(capsys.readouterr().out) == []


# ---- aggregation / prefixes / limit ----


def test_multiple_commits_counted_and_sorted(repo, coderepo, capsys):
    _make("a")  # ISSUE-1
    _make("b")  # ISSUE-2
    _commit(coderepo, "feat: a1 (ISSUE-1)")
    _commit(coderepo, "feat: b1 (ISSUE-2)")
    _commit(coderepo, "fix: a2 (ISSUE-1)")
    capsys.readouterr()
    _json(coderepo)
    data = json.loads(capsys.readouterr().out)
    # ISSUE-1 has 2 commits → sorts before ISSUE-2 (1 commit)
    assert [(r["id"], r["n_commits"]) for r in data] == [("ISSUE-1", 2), ("ISSUE-2", 1)]
    # newest commit for ISSUE-1 is listed first
    assert data[0]["commits"][0]["subject"] == "fix: a2 (ISSUE-1)"


def test_project_prefix_resolves_to_canonical(repo, coderepo, capsys):
    C.cmd_project_new("web", prefix="WEB")
    _make("web work", project="web")  # ISSUE-1, display WEB-1
    _commit(coderepo, "feat: ship (WEB-1)")  # display prefix in the commit
    capsys.readouterr()
    _json(coderepo)
    data = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in data] == ["WEB-1"]  # reported with the project prefix


def test_limit_caps_scanned_commits(repo, coderepo, capsys):
    _make("a")  # ISSUE-1
    _make("b")  # ISSUE-2
    _make("c")  # ISSUE-3
    _commit(coderepo, "feat: c1 (ISSUE-1)")
    _commit(coderepo, "feat: c2 (ISSUE-2)")
    _commit(coderepo, "feat: c3 (ISSUE-3)")
    capsys.readouterr()
    _json(coderepo, limit=2)  # only the 2 newest commits (c3, c2)
    data = json.loads(capsys.readouterr().out)
    assert {r["id"] for r in data} == {"ISSUE-2", "ISSUE-3"}


# ---- table output + errors ----


def test_table_none_message(repo, coderepo, capsys):
    _make("open one")
    _commit(coderepo, "chore: unrelated work")  # no ref
    capsys.readouterr()
    C.cmd_orphans(repo=str(coderepo))
    out = capsys.readouterr().out
    assert "orphans: none" in out


def test_table_lists_orphan(repo, coderepo, capsys):
    _make("open one")
    _commit(coderepo, "feat: work (ISSUE-1)")
    capsys.readouterr()
    C.cmd_orphans(repo=str(coderepo))
    out = capsys.readouterr().out
    assert "ISSUE-1" in out
    assert "引用但未关单" in out


def test_not_a_git_repo_errors(repo, tmp_path):
    nongit = tmp_path / "plain"
    nongit.mkdir()
    with pytest.raises(Exception, match="git log"):
        C.cmd_orphans(repo=str(nongit), as_json=True)
