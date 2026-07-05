"""Tests for the by-construction writing templates: `new --type`
default body skeletons, `--body` still overriding, `projects new` plan skeleton,
and the `groom` writing-health footer (unfilled sections + comment lengths).

Each runs against a throwaway DOCKET_ROOT (no git -> auto_commit is a no-op). The
autouse conftest fixture clears agent env markers, so `new` defaults to human
(no triage) unless a test opts in.
"""

from __future__ import annotations

import pytest

from docket import commands as C
from docket.commands import (
    _BODY_BUG,
    _BODY_TASK,
    _comment_block_lengths,
    _pct,
    _unfilled_sections,
    _writing_health,
)
from docket.errors import DocketError
from docket.issue import load_all, load_by_id, parse_issue


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


def _new(title="x", body="", type_="task", **kw):
    C.cmd_new(
        title,
        kw.get("project", ""),
        kw.get("priority", "No priority"),
        None,
        "",
        "",
        body,
        status=kw.get("status"),
        type_=type_,
    )


# ---- new --type: default body skeletons ----


def test_new_default_is_task_skeleton(repo):
    _new()  # no --type, no --body
    body = load_by_id("ISSUE-1").body
    for head in ("## 现状", "## 触发", "## 要做什么", "## 对人好处"):
        assert head in body, head
    assert "<!--" in body  # inline hints present
    assert "standing/治理/方法论任务" in body
    assert "优先编号清单" in body
    assert "具体动作差/机制差" in body
    # conditional sections are offered inside a comment (fill-or-delete), so they
    # are NOT live headings on a fresh skeleton.
    assert "「## 成本」" in body and "「## 下一步」" in body


def test_new_bug_type_skeleton(repo):
    _new(type_="bug")
    body = load_by_id("ISSUE-1").body
    for head in ("## 预期 vs 实际", "## 复现步骤 (MRE)", "## 环境", "## 证据"):
        assert head in body, head
    assert "只记一条就记这条" in body  # MRE hint
    # task-only headings must not leak into the bug skeleton
    assert "## 对人好处" not in body


def test_new_body_overrides_skeleton(repo):
    _new(body="just a one-liner")
    body = load_by_id("ISSUE-1").body
    assert "just a one-liner" in body
    assert "## 现状" not in body  # explicit body wins, no skeleton injected


def test_new_body_stdin_still_overrides(repo, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin body"))
    _new(body="-")
    body = load_by_id("ISSUE-1").body
    assert "from stdin body" in body
    assert "## 现状" not in body


def test_new_invalid_type_errors(repo):
    with pytest.raises(DocketError, match="invalid --type"):
        _new(type_="feature")


def test_new_invalid_type_errors_even_with_body(repo):
    # the flag is validated up-front, regardless of whether --body is given.
    with pytest.raises(DocketError, match="invalid --type"):
        _new(body="x", type_="bogus")


def test_skeleton_body_roundtrips_and_validates(repo, capsys):
    _new()
    is_ = load_by_id("ISSUE-1")
    # round-trip byte-identical (skeleton is plain body text, no special handling)
    from pathlib import Path

    original = Path(is_.path).read_text(encoding="utf-8")
    assert parse_issue(is_.path).render() == original
    capsys.readouterr()
    C.cmd_validate()  # non-empty body -> clean
    assert "OK" in capsys.readouterr().out


# ---- projects new: plan skeleton ----


def test_project_new_writes_plan_skeleton(repo):
    C.cmd_project_new("acme", title="Acme", prefix="ACME")
    text = (repo / "projects" / "acme.md").read_text(encoding="utf-8")
    assert "# Acme (ACME)" in text  # heading kept
    for head in ("## 目标", "## 为什么现在成一束", "## 范围·边界", "## done 口径"):
        assert head in text, head


# ---- writing-health helpers ----


def test_unfilled_sections_fresh_vs_filled(repo):
    assert _unfilled_sections("\n" + _BODY_TASK) == 4  # all four blank
    assert _unfilled_sections("\n" + _BODY_BUG) == 4
    # filling one section (even leaving the hint) clears it -> 3 remain.
    filled = ("\n" + _BODY_TASK).replace(
        "## 现状\n<!-- 静态背景:已确立的状态/配置,不含本轮触发 -->",
        "## 现状\n<!-- 静态背景:已确立的状态/配置,不含本轮触发 -->\ndocket 现无写作模板。",
    )
    assert _unfilled_sections(filled) == 3
    # a skeletonless body scores 0.
    assert _unfilled_sections("\njust prose, no headings\n") == 0


def test_comment_block_lengths(repo):
    _new()
    C.cmd_comment("ISSUE-1", "codex", "short one", session="")
    C.cmd_comment("ISSUE-1", "codex", "x" * 1500, session="")
    lengths = _comment_block_lengths("ISSUE-1")
    assert len(lengths) == 2
    assert min(lengths) == len("short one")
    assert max(lengths) == 1500
    assert _comment_block_lengths("ISSUE-1", root=str(repo)) == lengths


def test_pct_nearest_rank():
    assert _pct([], 50) == 0
    assert _pct([10, 20, 30, 40], 50) == 20
    assert _pct([10, 20, 30, 40], 95) == 40
    assert _pct([42], 50) == 42


def test_writing_health_aggregate(repo):
    _new(title="a")  # task skeleton, 4 unfilled
    _new(title="b")  # task skeleton, 4 unfilled
    C.cmd_comment("ISSUE-1", "codex", "x" * 1600, session="")  # warn
    C.cmd_comment("ISSUE-1", "codex", "x" * 2600, session="")  # warn + alarm
    C.cmd_comment("ISSUE-1", "codex", "tiny", session="")
    from docket.issue import find_repo_root

    h = _writing_health(load_all(), find_repo_root())
    assert h["unfilled_issues"] == 2
    assert h["unfilled_sections"] == 8
    assert h["comment_count"] == 3
    assert h["comment_max"] == 2600
    assert h["comment_warn"] == 2
    assert h["comment_alarm"] == 1


# ---- groom footer surfaces the writing-health signals ----


def test_groom_footer_shows_writing_health(repo, capsys):
    _new(title="a")
    _new(title="b")
    C.cmd_comment("ISSUE-1", "codex", "x" * 2600, session="")
    capsys.readouterr()
    C.cmd_groom(today_str="2026-07-02")
    out = capsys.readouterr().out
    assert "骨架未填: 2 条 issue 共 8 个 section" in out
    assert "comment 长度(字):" in out
    assert "warn(>1500): 1 条" in out
    assert "alarm(>2500): 1 条" in out
    assert (
        "长 comment 提示: 长 artifact 落 KB/docs/artifact;comment 只留链接 + 一句结论"
        in out
    )


def test_groom_json_shape_unchanged_by_health(repo, capsys):
    # the --json records must keep their exact per-issue key set (health is a
    # text-footer-only addition; agents consume the JSON).
    import json

    _new(title="a")
    capsys.readouterr()
    C.cmd_groom(as_json=True, today_str="2026-07-02")
    data = json.loads(capsys.readouterr().out)
    keys = {"id", "status", "age", "priority", "project", "parent", "comments", "title"}
    for rec in data:
        assert set(rec) == keys


# ---- CLI help surfaces --type + the title-format hint ----


def test_new_help_mentions_type_and_title_hint(repo):
    from typer.testing import CliRunner

    from docket.cli import app

    r = CliRunner().invoke(app, ["new", "--help"])
    assert r.exit_code == 0
    assert "--type" in r.output
    assert "标题" in r.output  # title-format hint on the argument
