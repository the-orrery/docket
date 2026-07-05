"""Tests for the docket Triage entry gate (ADR-008): the optional `triage: true`
field, the fail-closed `new` gate, read-time TTL self-heal, accept/decline/triage
verbs, work-surface exclusion, and the default_comment_actor env-name fix.

Each runs against a throwaway DOCKET_ROOT (no git → auto_commit is a no-op). The
autouse fixture in conftest.py clears agent env markers, so the gate default is
`human` unless a test sets a marker (or passes actor= / --triage) explicitly.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from docket import commands as C
from docket import projects as P
from docket.commands import default_comment_actor, read_comments, triage_pending
from docket.errors import ExitSignal
from docket.issue import load_all, load_by_id, parse_issue


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


def _new(title="x", **kw):
    """Create one issue, exposing the gate knobs (directed/triage/actor)."""
    C.cmd_new(
        title,
        kw.get("project", ""),
        kw.get("priority", "No priority"),
        kw.get("batch"),
        kw.get("milestone", ""),
        kw.get("parent", ""),
        "",
        status=kw.get("status"),
        blocked_by=kw.get("blocked_by"),
        directed=kw.get("directed", False),
        triage=kw.get("triage", None),
        actor=kw.get("actor", None),
    )


def _backdate_created(id_, date="2000-01-01"):
    """Force an issue's created date into the far past so its triage TTL is
    expired regardless of today (avoids monkeypatching today())."""
    is_ = load_by_id(id_)
    is_.set("created", date)
    is_.write()


# ---- ① gate: four paths ----


def test_gate_directed_no_triage(repo):
    # principal 点名建 → directed wins even under an agent actor.
    _new(directed=True, actor="claude")
    is_ = load_by_id("ISSUE-1")
    assert not is_.is_triage()
    assert is_.state_type() == "unstarted"  # state_type unchanged
    assert "triage:" not in is_.render()


def test_gate_explicit_triage_overrides_directed(repo):
    # --triage is highest priority, beats --directed.
    _new(triage=True, directed=True)
    is_ = load_by_id("ISSUE-1")
    assert is_.is_triage()
    assert is_.state_type() == "unstarted"  # still unstarted, not a 6th state_type
    assert "triage: true" in is_.render()


def test_gate_explicit_no_triage_overrides_agent(repo):
    # --no-triage beats the agent-context default.
    _new(triage=False, actor="codex")
    assert not load_by_id("ISSUE-1").is_triage()


def test_gate_agent_actor_defaults_triage(repo):
    # agent context (actor != human) → fail-closed into triage.
    _new(actor="claude")
    assert load_by_id("ISSUE-1").is_triage()


def test_gate_human_actor_no_triage(repo):
    # bare human terminal → straight to Todo.
    _new(actor="human")
    assert not load_by_id("ISSUE-1").is_triage()


def test_gate_default_is_human_in_clean_env(repo):
    # conftest cleared the markers → default_comment_actor() == human → no triage.
    _new()
    assert not load_by_id("ISSUE-1").is_triage()


def test_gate_agent_marker_env_triggers_triage(repo, monkeypatch):
    # a live agent marker in env makes the no-flag default fail-closed into triage.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    _new()
    assert load_by_id("ISSUE-1").is_triage()


# ---- ② TTL read-time self-heal ----


def test_triage_active_then_expired(repo):
    _new(triage=True)
    is_ = load_by_id("ISSUE-1")
    assert is_.is_triage() and is_.is_triage_active()
    assert not is_.is_triage_expired()
    # backdate created → past the 14d TTL → expired (read-time, not written).
    _backdate_created("ISSUE-1")
    is_ = load_by_id("ISSUE-1")
    assert is_.is_triage()  # field still on disk
    assert is_.is_triage_expired()
    assert not is_.is_triage_active()
    # the field was NOT rewritten (read-time self-heal, no daemon).
    assert "triage: true" in is_.render()


def test_expired_drops_from_inbox_and_nag_but_stays_hidden(repo, capsys):
    _new(title="aged proposal", triage=True)
    _backdate_created("ISSUE-1")
    # inbox/nag use is_triage_active → expired item drains out.
    assert triage_pending(load_all()) == []
    capsys.readouterr()
    C.cmd_triage()
    out = capsys.readouterr().out
    assert "aged proposal" not in out
    assert "inbox 空" in out
    # nag line absent (no active triage).
    C.cmd_active(False)
    assert "条待审" not in capsys.readouterr().out
    # still hidden from the work face (is_triage True regardless of TTL).
    C.cmd_active(False)
    assert "aged proposal" not in capsys.readouterr().out


# ---- ③ accept / decline ----


def test_accept_clears_field_and_enters_active(repo, capsys):
    _new(title="proposed work", triage=True)
    assert load_by_id("ISSUE-1").is_triage()
    C.cmd_accept("ISSUE-1")
    is_ = load_by_id("ISSUE-1")
    assert not is_.is_triage()
    assert "triage:" not in is_.render()
    assert (is_.status(), is_.state_type()) == ("Todo", "unstarted")
    capsys.readouterr()
    C.cmd_active(False)
    assert "proposed work" in capsys.readouterr().out


def test_accept_backlog(repo):
    _new(triage=True)
    C.cmd_accept("ISSUE-1", backlog=True)
    is_ = load_by_id("ISSUE-1")
    assert not is_.is_triage()
    assert (is_.status(), is_.state_type()) == ("Backlog", "backlog")


def test_accept_non_triage_errors(repo):
    _new(actor="human")  # not a triage item
    with pytest.raises(Exception, match="not in triage"):
        C.cmd_accept("ISSUE-1")


def test_decline_cancels_and_leaves_trace_comment(repo):
    _new(triage=True)
    C.cmd_decline("ISSUE-1", "duplicate of ISSUE-9")
    is_ = load_by_id("ISSUE-1")
    assert not is_.is_triage()
    assert (is_.status(), is_.state_type()) == ("Canceled", "canceled")
    # canceled (unlike completed) carries no completed date — the existing
    # canceled path removes it (validate would flag a canceled+completed pair).
    assert not is_.get("completed")[1]
    body, n = read_comments("ISSUE-1")
    assert n == 1
    assert "declined from triage: duplicate of ISSUE-9" in body


def test_decline_non_triage_errors(repo):
    _new(actor="human")
    with pytest.raises(Exception, match="not in triage"):
        C.cmd_decline("ISSUE-1", "nope")


def test_finish_clears_triage_field_and_inbox(repo):
    _new(title="already handled proposal", triage=True)
    C.cmd_finish("ISSUE-1")
    is_ = load_by_id("ISSUE-1")
    assert not is_.is_triage()
    assert (is_.status(), is_.state_type()) == ("Done", "completed")
    assert "triage:" not in is_.render()
    assert triage_pending(load_all()) == []


def test_status_change_clears_triage_field(repo):
    _new(title="accepted by direct state change", triage=True)
    C.cmd_set("ISSUE-1", status="started")
    is_ = load_by_id("ISSUE-1")
    assert not is_.is_triage()
    assert (is_.status(), is_.state_type()) == ("In Progress", "started")
    assert "triage:" not in is_.render()


# ---- triage inbox + --gc ----


def test_triage_inbox_lists_active_only(repo, capsys):
    _new(title="fresh proposal", triage=True)  # ISSUE-1 active
    _new(title="stale proposal", triage=True)  # ISSUE-2 → backdate to expire
    _backdate_created("ISSUE-2")
    capsys.readouterr()
    C.cmd_triage()
    out = capsys.readouterr().out
    assert "fresh proposal" in out
    assert "stale proposal" not in out


def test_triage_gc_materializes_expired(repo, capsys):
    _new(title="fresh", triage=True)
    _new(title="stale", triage=True)
    _backdate_created("ISSUE-2")
    capsys.readouterr()
    C.cmd_triage(gc=True)
    out = capsys.readouterr().out
    assert "1 条过期待审" in out
    # ISSUE-2 → canceled, triage field gone; ISSUE-1 untouched.
    g = load_by_id("ISSUE-2")
    assert g.state_type() == "canceled" and not g.is_triage()
    assert load_by_id("ISSUE-1").is_triage_active()


# ---- ④ work-surface exclusion ----


def test_triage_excluded_from_active_ready(repo, capsys):
    _new(title="real work", actor="human")  # ISSUE-1 Todo
    _new(title="proposal", triage=True)  # ISSUE-2 triage
    capsys.readouterr()
    C.cmd_active(False)
    out = capsys.readouterr().out
    assert "real work" in out and "proposal" not in out
    C.cmd_ready()
    out = capsys.readouterr().out
    assert "real work" in out and "proposal" not in out


def test_triage_active_all_shows_it(repo, capsys):
    _new(title="proposal", triage=True)
    capsys.readouterr()
    C.cmd_active(True)  # --all is the escape hatch
    assert "proposal" in capsys.readouterr().out


def test_triage_excluded_from_groom(repo, capsys):
    _new(title="real work", actor="human")
    _new(title="proposal", triage=True)
    capsys.readouterr()
    C.cmd_groom(today_str="2026-06-30")
    out = capsys.readouterr().out
    assert "real work" in out and "proposal" not in out


def test_triage_excluded_from_overview_and_progress(repo, capsys):
    C.cmd_project_new("p", title="P", prefix="P")
    _new(title="done work", project="p", actor="human")
    C.cmd_finish("ISSUE-1")
    _new(title="proposal", project="p", triage=True)  # ISSUE-2
    capsys.readouterr()
    P.cmd_overview()
    out = capsys.readouterr().out
    assert "proposal" not in out
    # denominator excludes triage: project shows 1/1 (done/total), not 1/2.
    assert "1/1" in out and "1/2" not in out


def test_triage_excluded_from_projects_active_and_scope(repo, capsys):
    C.cmd_project_new("p", title="P", prefix="P")
    _new(title="real work", project="p", actor="human")  # active, scope
    _new(title="proposal", project="p", triage=True)  # excluded
    capsys.readouterr()
    P.cmd_projects(False)
    out = capsys.readouterr().out
    # ACTIVE=1, DONE/SCOPE=0/1 (triage neither active nor scope).
    assert "0/1" in out and "0/2" not in out


# ---- ⑤ blocked_by → triage doesn't block ready ----


def test_triage_blocker_does_not_block_ready(repo, capsys):
    _new(title="proposal blocker", triage=True)  # ISSUE-1 triage
    _new(title="waiter", actor="human", blocked_by=["ISSUE-1"])  # ISSUE-2
    capsys.readouterr()
    C.cmd_ready()
    out = capsys.readouterr().out
    # the triage blocker is not a valid blocker → waiter is ready.
    assert "waiter" in out


# ---- ⑥ round-trip fidelity + validate ----


def test_no_triage_field_roundtrips_byte_identical(repo):
    _new(actor="human")
    is_ = load_by_id("ISSUE-1")
    assert not is_.get("triage")[1]
    assert "triage:" not in is_.render()
    original = Path(is_.path).read_text(encoding="utf-8")
    reparsed = parse_issue(is_.path).render()
    assert original == reparsed


def test_validate_accepts_triage_bool(repo, capsys):
    _new(triage=True)
    C.cmd_validate()  # must not raise
    assert "OK" in capsys.readouterr().out


def test_validate_flags_bad_triage(repo):
    _new(actor="human")
    is_ = load_by_id("ISSUE-1")
    is_.set_after("triage", "garbage", "project")
    is_.write()
    with pytest.raises(ExitSignal):
        C.cmd_validate()


# ---- ⑦ default_comment_actor env-name fix ----


@pytest.mark.parametrize(
    "marker", ["CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "AI_AGENT"]
)
def test_default_actor_detects_claude_markers(repo, monkeypatch, marker):
    monkeypatch.setenv(marker, "1")
    assert default_comment_actor() == "claude"


def test_default_actor_legacy_names_still_recognized(repo, monkeypatch):
    # backward compat: the old (never-real) names stay in the or-chain.
    monkeypatch.setenv("CLAUDE_SESSION_ID", "x")
    assert default_comment_actor() == "claude"


def test_default_actor_explicit_wins(repo, monkeypatch):
    monkeypatch.setenv("AI_AGENT", "1")
    monkeypatch.setenv("DOCKET_ACTOR", "alice")
    assert default_comment_actor() == "alice"


# ---- `docket list --triage` audit lens (incl. expired) + bare-list marker ----


def test_list_triage_flag_lists_all_triage_incl_expired(repo, capsys):
    _new(title="real work", actor="human")  # ISSUE-1, not triage
    _new(title="fresh proposal", triage=True)  # ISSUE-2 active
    _new(title="stale proposal", triage=True)  # ISSUE-3 → expired
    _backdate_created("ISSUE-3")
    capsys.readouterr()
    C.cmd_list("", "", "", "", "", triage=True)
    out = capsys.readouterr().out
    assert "fresh proposal" in out
    assert "stale proposal" in out  # expired triage still listed (audit lens)
    assert "real work" not in out  # non-triage excluded by the filter


def test_bare_list_marks_triage_rows(repo, capsys):
    _new(title="proposal", triage=True)
    capsys.readouterr()
    C.cmd_list("", "", "", "", "")  # bare list shows everything…
    out = capsys.readouterr().out
    assert "proposal" in out
    assert "📥" in out  # …but triage rows carry a marker (don't `start` by mistake)


# ---- progress_counts excludes triage (same 口径 as overview/projects) ----


def test_progress_counts_excludes_triage(repo):
    from docket.projects import progress_counts

    C.cmd_project_new("p", title="P", prefix="P")
    _new(title="done", project="p", actor="human")  # ISSUE-1
    C.cmd_finish("ISSUE-1")
    _new(title="proposal", project="p", triage=True)  # ISSUE-2 (out of scope)
    mine = [i for i in load_all() if i.project() == "p"]
    assert progress_counts(mine) == (1, 1)  # triage neither done nor scope


def test_project_drill_progress_matches_overview(repo, capsys):
    C.cmd_project_new("p", title="P", prefix="P")
    _new(title="done", project="p", actor="human")
    C.cmd_finish("ISSUE-1")
    _new(title="proposal", project="p", triage=True)
    capsys.readouterr()
    P.cmd_project("p")  # single-project drill
    out = capsys.readouterr().out
    assert "1/1" in out and "1/2" not in out  # same ratio overview now shows
    assert "proposal" not in out  # ordinary project groups hide triage proposals


# ---- TUI: triage parent filtered from work buckets must not orphan its child ----


def test_ui_triage_parent_keeps_child_as_context(repo):
    import asyncio

    from textual.widgets import ListView

    from docket.ui import PMUI, ContextItem, IssueItem

    # triage (unstarted) PARENT + non-triage started CHILD.
    _new(title="triage parent", triage=True)  # ISSUE-1
    _new(title="real child", actor="human", parent="ISSUE-1", status="started")  # 2

    async def go():
        app = PMUI(roots=[("t", str(repo))])
        async with app.run_test(size=(124, 40)) as pilot:
            await pilot.pause()
            lv = app.query_one("#batch-issues", ListView)  # 进行中 bucket loaded
            titles = [c.issue.title() for c in lv.children if isinstance(c, IssueItem)]
            has_ctx = any(isinstance(c, ContextItem) for c in lv.children)
            return titles, has_ctx, len(app.buckets)

    titles, has_ctx, nbuckets = asyncio.run(go())
    assert "real child" in titles  # child still rendered (not orphaned)
    assert "triage parent" not in titles  # parent filtered from the work bucket
    assert has_ctx  # …but shown as a disabled context row up the tree
    assert nbuckets == 7  # 6 work buckets + the new 待审 bucket


def test_ui_open_and_project_surfaces_hide_triage(repo):
    import asyncio

    from textual.widgets import ListView

    from docket.ui import PMUI, IssueItem

    C.cmd_project_new("p", title="P", prefix="P")
    _new(title="real work", project="p", actor="human")
    _new(title="proposal", project="p", triage=True)

    async def go():
        app = PMUI(roots=[("t", str(repo))])
        async with app.run_test(size=(124, 40)) as pilot:
            await pilot.pause()
            await app._load_open()
            open_lv = app.query_one("#batch-issues", ListView)
            open_titles = [
                c.issue.title() for c in open_lv.children if isinstance(c, IssueItem)
            ]
            await app._load_project("p")
            proj_lv = app.query_one("#proj-issues", ListView)
            project_titles = [
                c.issue.title() for c in proj_lv.children if isinstance(c, IssueItem)
            ]
            return open_titles, project_titles

    open_titles, project_titles = asyncio.run(go())
    assert "real work" in open_titles and "proposal" not in open_titles
    assert "real work" in project_titles and "proposal" not in project_titles
