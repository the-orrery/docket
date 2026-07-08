"""Tests for the feature additions: new/set --status, comment
--amend/--delete-last, and get <id> <field>. Each runs against a throwaway
DOCKET_ROOT (no git → auto_commit is a no-op)."""

from __future__ import annotations

import pytest

from docket import commands as C
from docket.commands import (
    _comment_block,
    _last_comment_start,
    default_comment_actor,
    default_comment_session,
    read_comments,
)
from docket.issue import load_by_id, quote_scalar


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    # hermetic: don't inherit DOCKET_ID_PREFIX from the dev's env, else writes
    # normalize to it while these tests assert the default ISSUE-<n>.
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


def _make(title="x", **kw):
    C.cmd_new(
        title,
        kw.get("project", ""),
        kw.get("priority", "No priority"),
        None,
        "",
        "",
        "",
        status=kw.get("status"),
    )


# ---- new --status ----


def test_new_default_is_todo(repo):
    _make()
    is_ = load_by_id("ISSUE-1")
    assert (is_.status(), is_.state_type()) == ("Todo", "unstarted")


def test_new_status_lands_in_backlog(repo):
    _make(status="backlog")
    is_ = load_by_id("ISSUE-1")
    assert (is_.status(), is_.state_type()) == ("Backlog", "backlog")


def test_new_status_accepts_display_name(repo):
    _make(status="In Progress")
    is_ = load_by_id("ISSUE-1")
    assert is_.state_type() == "started"


# ---- set --status ----


def test_set_status_pairs_completed_date(repo):
    _make()
    C.cmd_set("ISSUE-1", status="Done")
    is_ = load_by_id("ISSUE-1")
    assert is_.state_type() == "completed"
    assert is_.get("completed")[1]  # completed date stamped on entering completed
    # leaving completed clears the date (keeps validate consistent)
    C.cmd_set("ISSUE-1", status="started")
    is_ = load_by_id("ISSUE-1")
    assert is_.state_type() == "started"
    assert not is_.get("completed")[1]


def test_set_status_with_other_fields_one_call(repo):
    _make()
    C.cmd_set("ISSUE-1", status="backlog", priority="High")
    is_ = load_by_id("ISSUE-1")
    assert is_.state_type() == "backlog"
    assert is_.priority() == "High"


# ---- get ----


def test_get_field(repo, capsys):
    _make(title="hello title", priority="High")
    capsys.readouterr()  # drain the "created ..." line from cmd_new
    C.cmd_get("ISSUE-1", "priority")
    assert capsys.readouterr().out.strip() == "High"
    C.cmd_get("ISSUE-1", "title")
    assert capsys.readouterr().out.strip() == "hello title"


def test_get_missing_field_errors(repo):
    _make()
    with pytest.raises(Exception, match="no frontmatter field"):
        C.cmd_get("ISSUE-1", "nonesuch")


# ---- wake (snooze) ----


def test_new_without_wake_has_no_field(repo):
    # the field must never appear unless explicitly set (round-trip fidelity).
    _make()
    is_ = load_by_id("ISSUE-1")
    assert not is_.get("wake")[1]
    assert "wake:" not in is_.render()


def test_set_wake_lands_after_milestone(repo):
    _make()
    C.cmd_set("ISSUE-1", milestone="M1", wake="2026-07-01")
    is_ = load_by_id("ISSUE-1")
    assert is_.wake() == "2026-07-01"
    keys = [k for k, _ in is_.fields]
    # project, milestone, wake, parent — wake sits between milestone and parent
    assert keys.index("wake") == keys.index("milestone") + 1
    assert keys.index("wake") < keys.index("parent")


def test_new_wake_places_field(repo):
    C.cmd_new(
        "with wake", "", "No priority", None, "", "", "", status=None, wake="2026-07-01"
    )
    is_ = load_by_id("ISSUE-1")
    assert is_.wake() == "2026-07-01"


def test_unwake_removes_field(repo):
    _make()
    C.cmd_set("ISSUE-1", wake="2026-07-01")
    assert load_by_id("ISSUE-1").get("wake")[1]
    C.cmd_set("ISSUE-1", unwake=True)
    is_ = load_by_id("ISSUE-1")
    assert not is_.get("wake")[1]
    assert "wake:" not in is_.render()


def test_set_wake_rejects_bad_date(repo):
    _make()
    with pytest.raises(Exception, match="invalid wake"):
        C.cmd_set("ISSUE-1", wake="2026-13-40")


def test_new_wake_rejects_bad_date(repo):
    with pytest.raises(Exception, match="invalid wake"):
        C.cmd_new(
            "x", "", "No priority", None, "", "", "", status=None, wake="notadate"
        )


def test_validate_flags_bad_wake(repo):
    _make()
    is_ = load_by_id("ISSUE-1")
    is_.set_after("wake", "garbage", "project")
    is_.write()
    with pytest.raises(  # noqa: B017
        Exception
    ):  # ExitSignal(1) — validate found a problem
        C.cmd_validate()


def _future():
    from docket.issue import cn_now

    return (cn_now().replace(year=cn_now().year + 1)).strftime("%Y-%m-%d")


def _past():
    return "2000-01-01"


def test_is_snoozed_future_only(repo):
    _make()  # unstarted (open)
    is_ = load_by_id("ISSUE-1")
    assert not is_.is_snoozed()  # no wake
    C.cmd_set("ISSUE-1", wake=_future())
    assert load_by_id("ISSUE-1").is_snoozed()
    C.cmd_set("ISSUE-1", wake=_past())
    is_ = load_by_id("ISSUE-1")
    assert not is_.is_snoozed()  # past wake is not "asleep"
    assert is_.is_awake_due()  # it's "睡醒待看"


def test_completed_issue_never_snoozed(repo):
    _make()
    C.cmd_set("ISSUE-1", wake=_future())
    C.cmd_set("ISSUE-1", status="Done")
    is_ = load_by_id("ISSUE-1")
    # a finished issue with a stale future wake must not register as asleep/due
    assert not is_.is_snoozed()
    assert not is_.is_awake_due()


def test_active_hides_future_wake(repo, capsys):
    _make(title="awake one")
    C.cmd_new("sleepy", "", "No priority", None, "", "", "", status=None)
    C.cmd_set("ISSUE-2", wake=_future())
    capsys.readouterr()
    C.cmd_active(False)
    out = capsys.readouterr().out
    assert "awake one" in out
    assert "sleepy" not in out  # snoozed → hidden
    # --all shows it again
    C.cmd_active(True)
    assert "sleepy" in capsys.readouterr().out


def test_ready_keeps_future_wake(repo, capsys):
    _make(title="sleepy")
    C.cmd_set("ISSUE-1", wake=_future())
    capsys.readouterr()
    C.cmd_ready()
    out = capsys.readouterr().out
    assert "sleepy" in out


def test_active_shows_due_line(repo, capsys):
    _make()
    C.cmd_set("ISSUE-1", wake=_past())
    capsys.readouterr()
    C.cmd_active(False)
    out = capsys.readouterr().out
    assert "到期待看" in out and "ISSUE-1" in out


def test_no_due_line_when_none(repo, capsys):
    _make()
    capsys.readouterr()
    C.cmd_active(False)
    assert "到期待看" not in capsys.readouterr().out


def test_batch_excludes_triage_from_frontier_and_numeric_view(repo, capsys):
    C.cmd_new("proposal", "", "No priority", 1, "", "", "", triage=True)
    C.cmd_new("current work", "", "No priority", 2, "", "", "", status=None)
    C.cmd_new("next work", "", "No priority", 3, "", "", "", status=None)
    capsys.readouterr()

    C.cmd_batch(None)
    out = capsys.readouterr().out
    assert "本批 · batch 2" in out
    assert "下批 · batch 3" in out
    assert "current work" in out and "next work" in out
    assert "proposal" not in out

    C.cmd_batch("1")
    out = capsys.readouterr().out
    assert out.strip() == "(no issues in batch 1)"


def test_roll_ignores_triage_batches(repo, capsys):
    C.cmd_new("proposal", "", "No priority", 1, "", "", "", triage=True)
    C.cmd_new("current work", "", "No priority", 2, "", "", "", status=None)
    C.cmd_new("next work", "", "No priority", 3, "", "", "", status=None)
    capsys.readouterr()

    C.cmd_roll(yes=True)
    out = capsys.readouterr().out
    assert "rolled: batch 3 → 2" in out
    assert load_by_id("ISSUE-1").batch() == 1
    assert load_by_id("ISSUE-2").batch() == 2
    assert load_by_id("ISSUE-3").batch() == 2


# ---- finish asset reminder (ISSUE-416) ----


def test_finish_prints_asset_reminder(repo, capsys):
    _make()
    capsys.readouterr()  # drain cmd_new output
    C.cmd_finish("ISSUE-1")
    cap = capsys.readouterr()
    # machine-readable status line stays on stdout (scripts grep "-> Done")
    assert cap.out.strip() == "ISSUE-1 -> Done (completed)"
    # the four-question 资产自检 lands on stderr, not stdout
    assert "资产自检" in cap.err
    for q in ("拍板了方案/方向", "定了约束/口径", "系统架构变了", "踩了新坑"):
        assert q in cap.err
    assert "资产自检" not in cap.out


def test_start_does_not_print_reminder(repo, capsys):
    _make()
    capsys.readouterr()
    C.cmd_start("ISSUE-1")
    cap = capsys.readouterr()
    assert "资产自检" not in cap.out
    assert "资产自检" not in cap.err


def test_cancel_does_not_print_reminder(repo, capsys):
    _make()
    capsys.readouterr()
    C.cmd_status("ISSUE-1", "canceled")
    cap = capsys.readouterr()
    assert "资产自检" not in cap.out
    assert "资产自检" not in cap.err


def test_finish_reminder_suppressed_by_env(repo, capsys, monkeypatch):
    _make()
    capsys.readouterr()
    monkeypatch.setenv("DOCKET_NO_ASSET_REMINDER", "1")
    C.cmd_finish("ISSUE-1")
    cap = capsys.readouterr()
    assert cap.out.strip() == "ISSUE-1 -> Done (completed)"
    assert cap.err == ""


# ---- comment append / amend / delete-last ----


def test_comment_append_amend_delete(repo):
    _make()
    C.cmd_comment("ISSUE-1", "codex", "first", session="")
    C.cmd_comment("ISSUE-1", "codex", "second", session="")
    body, n = read_comments("ISSUE-1")
    assert n == 2
    assert "first" in body and "second" in body
    assert "## codex ·" not in body
    assert "## " in body and " · codex\n\n" in body

    # amend replaces the last block (count stays 2, not 3)
    C.cmd_comment("ISSUE-1", "codex", "second-fixed", amend=True, session="")
    body, n = read_comments("ISSUE-1")
    assert n == 2
    assert "first" in body and "second-fixed" in body

    # delete-last drops the last block
    C.cmd_comment("ISSUE-1", "codex", "", delete_last=True, session="")
    body, n = read_comments("ISSUE-1")
    assert n == 1
    assert "first" in body and "second-fixed" not in body


def test_comment_amend_empty_repo_errors(repo):
    _make()
    with pytest.raises(Exception, match="no comments"):
        C.cmd_comment("ISSUE-1", "codex", "x", amend=True, session="")


def test_read_comments_root_pins_lane(repo, tmp_path, monkeypatch):
    """Multi-tier regression: the same canonical id can exist in two lanes whose
    comments live in their own repos. read_comments(id, root=lane) must read the
    pinned lane regardless of the global DOCKET_ROOT — without it the aggregated
    TUI resolved every issue's comments against one (often wrong/empty) root."""
    # lane A = the fixture's DOCKET_ROOT.
    _make()
    C.cmd_comment("ISSUE-1", "codex", "in-lane-A", session="")
    # lane B = a second repo with the same id but a different comment.
    lane_b = tmp_path / "laneB"
    (lane_b / "issues").mkdir(parents=True)
    monkeypatch.setenv("DOCKET_ROOT", str(lane_b))
    _make()
    C.cmd_comment("ISSUE-1", "codex", "in-lane-B", session="")
    # Global root points back at lane A (as cwd would resolve post-aggregation).
    monkeypatch.setenv("DOCKET_ROOT", str(repo))
    body, n = read_comments("ISSUE-1")
    assert n == 1 and "in-lane-A" in body
    body, n = read_comments("ISSUE-1", root=str(lane_b))
    assert n == 1 and "in-lane-B" in body and "in-lane-A" not in body


def test_last_comment_start():
    s = "head\n## a\n\nx\n\n---\n\n## b\n\ny\n"
    assert s[_last_comment_start(s) :].startswith("## b")
    assert _last_comment_start("no blocks") == -1
    assert _last_comment_start("## only\n\nx\n") == 0


# ---- blocked_by dependency edges ----


def test_set_blocked_by_roundtrip(repo):
    _make("blocker")
    _make("waiter")
    C.cmd_set("ISSUE-2", blocked_by=["1"])
    is_ = load_by_id("ISSUE-2")
    assert is_.blocked_by() == ["ISSUE-1"]
    # idempotent add, then a second edge
    _make("other")
    C.cmd_set("ISSUE-2", blocked_by=["ISSUE-1", "ISSUE-3"])
    assert load_by_id("ISSUE-2").blocked_by() == ["ISSUE-1", "ISSUE-3"]
    # unblock removes one edge; removing the last drops the field
    C.cmd_set("ISSUE-2", unblock=["ISSUE-3"])
    assert load_by_id("ISSUE-2").blocked_by() == ["ISSUE-1"]
    C.cmd_set("ISSUE-2", unblock=["ISSUE-1"])
    is_ = load_by_id("ISSUE-2")
    assert is_.blocked_by() == []
    assert not is_.get("blocked_by")[1]


def test_set_blocked_by_rejects_self_and_missing(repo):
    _make("a")
    with pytest.raises(Exception, match="itself"):
        C.cmd_set("ISSUE-1", blocked_by=["ISSUE-1"])
    with pytest.raises(Exception, match="does not reference"):
        C.cmd_set("ISSUE-1", blocked_by=["ISSUE-99"])
    with pytest.raises(Exception, match="not blocked by"):
        C.cmd_set("ISSUE-1", unblock=["ISSUE-99"])


def test_new_with_blocked_by(repo):
    _make("blocker")
    C.cmd_new("waiter", "", "No priority", None, "", "", "", blocked_by=["1"])
    assert load_by_id("ISSUE-2").blocked_by() == ["ISSUE-1"]


def test_open_blockers_and_ready(repo, capsys):
    _make("blocker")
    _make("waiter")
    _make("free")
    C.cmd_set("ISSUE-2", blocked_by=["ISSUE-1"])
    capsys.readouterr()
    C.cmd_ready()
    out = capsys.readouterr().out
    assert "blocker" in out and "free" in out
    assert "waiter" not in out
    # blocker done -> waiter becomes ready (edge resolves by computation)
    C.cmd_set("ISSUE-1", status="Done")
    capsys.readouterr()
    C.cmd_ready()
    out = capsys.readouterr().out
    assert "waiter" in out and "blocker" not in out


def test_show_renders_both_directions(repo, capsys):
    _make("blocker")
    _make("waiter")
    C.cmd_set("ISSUE-2", blocked_by=["ISSUE-1"])
    capsys.readouterr()
    C.cmd_show("ISSUE-2", no_comments=True)
    out = capsys.readouterr().out
    assert "blocked_by:" in out and "ISSUE-1" in out
    C.cmd_show("ISSUE-1", no_comments=True)
    out = capsys.readouterr().out
    assert "blocks:" in out and "ISSUE-2" in out


def test_validate_flags_dangling_and_cycle(repo, capsys):
    _make("a")
    _make("b")
    from docket.errors import ExitSignal

    C.cmd_set("ISSUE-1", blocked_by=["ISSUE-2"])
    C.cmd_set("ISSUE-2", blocked_by=["ISSUE-1"])
    with pytest.raises(ExitSignal):
        C.cmd_validate()
    err = capsys.readouterr().err
    assert "cycle" in err
    # break the cycle -> clean
    C.cmd_set("ISSUE-2", unblock=["ISSUE-1"])
    capsys.readouterr()
    C.cmd_validate()
    assert "OK" in capsys.readouterr().out


def test_validate_flags_unknown_project_by_default(repo, capsys):
    _make("external drift")
    is_ = load_by_id("ISSUE-1")
    is_.set("project", quote_scalar("ghost"))
    is_.write()
    from docket.errors import ExitSignal

    with pytest.raises(ExitSignal):
        C.cmd_validate()
    err = capsys.readouterr().err
    assert 'project "ghost" does not reference an existing project' in err


def test_validate_strict_flags_in_scope_issue_without_project(repo, capsys):
    _make("needs project")
    C.cmd_validate()
    assert "validate: OK" in capsys.readouterr().out
    from docket.errors import ExitSignal

    with pytest.raises(ExitSignal):
        C.cmd_validate(strict=True)
    err = capsys.readouterr().err
    assert "project is empty" in err
    assert "strict mode requires project" in err


def test_validate_strict_allows_canceled_issue_without_project(repo, capsys):
    _make("out of scope", status="Canceled")
    C.cmd_validate(strict=True)
    assert "validate: OK (strict)" in capsys.readouterr().out


def test_comment_actor_session_defaults(monkeypatch):
    monkeypatch.delenv("DOCKET_COMMENT_ACTOR", raising=False)
    monkeypatch.delenv("DOCKET_ACTOR", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "session-123")
    assert default_comment_actor() == "codex"
    assert default_comment_session() == "session-123"
    block = _comment_block("", "hello")
    assert block.startswith("## ")
    assert " · codex · session session-123\n\nhello" in block

    monkeypatch.setenv("DOCKET_COMMENT_ACTOR", "human")
    monkeypatch.setenv("DOCKET_COMMENT_SESSION", "manual-456")
    assert default_comment_actor() == "human"
    assert default_comment_session() == "manual-456"
