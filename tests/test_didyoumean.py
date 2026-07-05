"""did-you-mean hints on the two densest agent miscalls:

1. unknown subcommand where a *later* token is a real verb — the
   "noun-before-verb" mistake `docket issue show <id>` (audit's top error).
2. an id that resolves to a non-existent issue — suggest the nearest real ids.

Lexically-close subcommand typos (`shwo`→`show`) are already handled by
TyperGroup itself; we only assert that path stays intact.
"""

from __future__ import annotations

import pytest

from docket import cli
from docket import commands as C
from docket.cli import _command_suggestion
from docket.errors import DocketError
from docket.issue import _closest_ids, load_by_id

_UsageError = cli._click_exc.UsageError


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


def _make(title="x"):
    C.cmd_new(title, "", "No priority", None, "", "", "", status=None)


# ---- hint 1: unknown subcommand → noun-before-verb suggestion ----


def test_command_suggestion_picks_known_verb_in_rest():
    cmds = ["show", "set", "list"]
    assert (
        _command_suggestion("issue", cmds, ["show", "ISSUE-1"], "docket")
        == "Did you mean: 'show'? (try: docket show ISSUE-1)"
    )


def test_command_suggestion_uses_invoker_prog():
    assert "(try: pm show ISSUE-1)" in _command_suggestion(
        "issue", ["show"], ["show", "ISSUE-1"], "pm"
    )


def test_command_suggestion_none_when_no_known_verb_follows():
    cmds = ["show", "set"]
    assert _command_suggestion("issue", cmds, ["nope", "x"], "docket") is None
    assert _command_suggestion("issue", cmds, [], "docket") is None


def test_unknown_subcommand_suggests_following_verb():
    with pytest.raises(_UsageError) as ei:
        cli._cli.main(
            ["issue", "show", "ISSUE-1"], prog_name="docket", standalone_mode=False
        )
    msg = ei.value.message
    assert "Did you mean: 'show'?" in msg
    assert "docket show ISSUE-1" in msg


def test_lexical_typo_suggestion_still_works():
    # TyperGroup's own close-match path is untouched (no following verb here).
    with pytest.raises(_UsageError) as ei:
        cli._cli.main(["shwo", "ISSUE-1"], prog_name="docket", standalone_mode=False)
    assert "show" in ei.value.message


def test_truly_unknown_first_token_stays_bare():
    with pytest.raises(_UsageError) as ei:
        cli._cli.main(["zzzzz"], prog_name="docket", standalone_mode=False)
    assert "Did you mean" not in ei.value.message


# ---- hint 2: not-found id → nearest existing ids ----


def test_closest_ids_ranks_nearest_number_first():
    existing = ["ISSUE-1", "ISSUE-2", "ISSUE-3", "ISSUE-40"]
    assert "ISSUE-40" in _closest_ids("ISSUE-4", existing)


def test_closest_ids_empty_when_nothing_exists():
    assert _closest_ids("ISSUE-9", []) == []


def test_load_missing_id_suggests_existing(repo):
    _make()  # ISSUE-1
    with pytest.raises(DocketError) as ei:
        load_by_id("ISSUE-9")
    msg = ei.value.message
    assert "issue ISSUE-9 not found" in msg
    assert "Did you mean: ISSUE-1" in msg


def test_wrong_prefix_normalizes_then_suggests(repo):
    # DEMO-9 normalizes to the canonical ISSUE-9 (prefix is display-only); when
    # that number is absent the hint points at the nearest real id.
    _make()  # ISSUE-1
    with pytest.raises(DocketError) as ei:
        load_by_id("DEMO-9")
    assert "issue ISSUE-9 not found. Did you mean: ISSUE-1?" == ei.value.message


def test_load_missing_id_no_suggestion_in_empty_repo(repo):
    with pytest.raises(DocketError) as ei:
        load_by_id("ISSUE-9")
    assert "Did you mean" not in ei.value.message
