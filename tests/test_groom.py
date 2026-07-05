"""Tests for `docket groom` — the deterministic staleness inventory of every
non-done issue. Each runs against a throwaway DOCKET_ROOT (no git → auto_commit is
a no-op) with --today pinned so age math / sort / filtering are reproducible."""

from __future__ import annotations

import datetime
import json

import pytest

from docket import commands as C
from docket.commands import (
    _groom_rows,
    _long_work_health,
    _work_health,
    collect_validation_problems,
)
from docket.issue import load_all, load_by_id
from docket.projects import load_projects

_TODAY = datetime.date(2026, 6, 25)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    # hermetic: don't inherit DOCKET_ID_PREFIX from the dev's env, else display
    # ids won't resolve to the default ISSUE-<n> these tests assert.
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


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


def _set_updated(id_, date):
    """Pin an issue's `updated` date directly (bypassing cmd_set's today() bump)."""
    is_ = load_by_id(id_)
    is_.set("updated", date)
    is_.write()


def _set_body(id_, body):
    is_ = load_by_id(id_)
    is_.body = body
    is_.write()


def _rows(project=""):
    issues = load_all()
    if project:
        issues = [is_ for is_ in issues if project in is_.project()]
    return _groom_rows(issues, load_projects()[0], _TODAY)


# ---- non-done filtering ----


def test_groom_excludes_done_and_canceled(repo):
    _make("alive todo")
    _make("done one")
    _make("canceled one")
    C.cmd_set("ISSUE-2", status="Done")
    C.cmd_set("ISSUE-3", status="canceled")
    ids = {r["id"] for r in _rows()}
    assert ids == {"ISSUE-1"}  # Done/Canceled dropped


def test_groom_backlog_is_non_done(repo):
    _make("backlogged", status="backlog")
    assert [r["status"] for r in _rows()] == ["Backlog"]


def test_groom_includes_future_wake(repo):
    _make("sleepy")
    C.cmd_set("ISSUE-1", wake="2999-01-01")
    rows = _rows()
    assert [r["id"] for r in rows] == ["ISSUE-1"]


# ---- age (today − updated) ----


def test_groom_age_from_today(repo):
    _make("t")
    _set_updated("ISSUE-1", "2026-06-01")
    assert _rows()[0]["age"] == 24  # 2026-06-25 − 2026-06-01


def test_groom_today_flag_overrides(repo, capsys):
    _make("t")
    _set_updated("ISSUE-1", "2026-06-10")
    capsys.readouterr()
    C.cmd_groom(today_str="2026-06-20")
    out = capsys.readouterr().out
    # age = 10 days appears in the row
    assert "10" in out and "ISSUE-1" in out


def test_groom_bad_today_errors(repo):
    _make("t")
    with pytest.raises(Exception, match="invalid --today"):
        C.cmd_groom(today_str="2026-13-40")


# ---- sort: status group then age desc ----


def test_groom_sort_status_group_then_age_desc(repo):
    _make("ip", status="In Progress")  # ISSUE-1
    _make("todo old")  # ISSUE-2 Todo
    _make("todo new")  # ISSUE-3 Todo
    _make("backlog one", status="backlog")  # ISSUE-4
    _set_updated("ISSUE-1", "2026-06-20")
    _set_updated("ISSUE-2", "2026-06-01")  # older Todo → first within Todo
    _set_updated("ISSUE-3", "2026-06-15")  # newer Todo
    _set_updated("ISSUE-4", "2026-06-10")
    rows = _rows()
    # status groups in fixed order: In Progress, then Todo (older first), then Backlog
    assert [r["id"] for r in rows] == ["ISSUE-1", "ISSUE-2", "ISSUE-3", "ISSUE-4"]
    assert [r["status"] for r in rows] == [
        "In Progress",
        "Todo",
        "Todo",
        "Backlog",
    ]


# ---- --project filter ----


def test_groom_project_filter(repo):
    C.cmd_project_new("web", prefix="WEB")
    C.cmd_project_new("api", prefix="API")
    _make("in web", project="web")
    _make("in api", project="api")
    assert [r["project"] for r in _rows(project="web")] == ["web"]


# ---- --json shape ----


def test_groom_json_shape(repo, capsys):
    C.cmd_project_new("web", prefix="WEB")
    _make("titled", project="web", priority="High", status="In Progress")
    _make("child", project="web", parent="ISSUE-1")
    _set_updated("ISSUE-1", "2026-06-05")
    capsys.readouterr()
    C.cmd_groom(as_json=True, today_str="2026-06-25")
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    keys = {"id", "status", "age", "priority", "project", "parent", "comments", "title"}
    for rec in data:
        assert set(rec) == keys
    first = data[0]  # In Progress sorts first
    assert first["id"] == "WEB-1"  # display id uses the project prefix
    assert first["status"] == "In Progress"
    assert first["age"] == 20
    assert first["priority"] == "High"
    assert first["parent"] == "-"  # ~ rendered as -
    child = next(r for r in data if r["id"] == "WEB-2")
    assert child["parent"] == "ISSUE-1"  # canonical parent id


def test_groom_json_comment_count(repo, capsys):
    _make("t")
    C.cmd_comment("ISSUE-1", "codex", "one", session="")
    C.cmd_comment("ISSUE-1", "codex", "two", session="")
    capsys.readouterr()
    C.cmd_groom(as_json=True, today_str="2026-06-25")
    data = json.loads(capsys.readouterr().out)
    assert data[0]["comments"] == 2


# ---- footer: validate summary + non-done total ----


def test_groom_footer_validate_ok_and_total(repo, capsys):
    _make("a")
    _make("b")
    _make("done", status="In Progress")
    C.cmd_set("ISSUE-3", status="Done")
    capsys.readouterr()
    C.cmd_groom(today_str="2026-06-25")
    out = capsys.readouterr().out
    assert "validate: OK" in out
    assert "non-done: 2" in out  # ISSUE-3 is Done → excluded from the count


def test_groom_footer_reports_validate_problems(repo, capsys):
    _make("a")
    _make("b")
    # introduce a data problem: dangling blocked_by ref (validate flags it)
    is_ = load_by_id("ISSUE-1")
    is_.set_blocked_by(["ISSUE-99"])
    is_.write()
    assert collect_validation_problems(load_all())  # precondition: it's a problem
    capsys.readouterr()
    C.cmd_groom(today_str="2026-06-25")
    out = capsys.readouterr().out
    assert "validate:" in out and "problem(s)" in out


def test_groom_empty_repo_message(repo, capsys):
    capsys.readouterr()
    C.cmd_groom(today_str="2026-06-25")
    out = capsys.readouterr().out
    assert "(no non-done issues)" in out
    assert "non-done: 0" in out


# ---- footer: long-running work soft health hints ----


def test_long_work_health_detects_umbrella_missing_structure(repo):
    _make("umbrella")
    _make("child one", parent="ISSUE-1")
    _make("child two", parent="ISSUE-1")

    structured = _work_health(load_all(), load_projects()[0])
    health = _long_work_health(load_all(), load_projects()[0])

    assert structured["schema_version"] == 1
    assert structured["source"] == "docket.work_health"
    assert structured["scope"] == {"project": ""}
    assert structured["summary"] == {
        "long_work_candidates": 1,
        "signal_count": 4,
    }
    assert [s["kind"] for s in structured["signals"]] == [
        "missing_status_card",
        "missing_stage_exit",
        "missing_next_action",
        "unclear_implementation_gate",
    ]
    for signal in structured["signals"]:
        assert set(signal) == {
            "issue_id",
            "display_id",
            "title",
            "kind",
            "label",
            "severity",
            "confidence",
            "reason",
            "recommended_action",
            "field",
        }
        assert signal["issue_id"] == "ISSUE-1"
        assert signal["display_id"] == "ISSUE-1"
        assert signal["severity"] == "advisory"
        assert signal["confidence"] == "high"

    assert health["candidates"] == 1
    assert health["hints"] == [
        {
            "id": "ISSUE-1",
            "hints": [
                "缺当前状态卡",
                "缺阶段出口",
                "缺下一步最小动作",
                "实现闸门不清",
            ],
        }
    ]


def test_long_work_health_accepts_complete_status_card(repo):
    _make("长期控制面")
    _set_body(
        "ISSUE-1",
        """## 当前状态卡

- 当前阶段：P1 health hint。
- 下一步最小动作：跑一次 groom。
- 进入实现闸门：不改 schema，不迁数据模型。
- 不做什么：不做大型 TUI。

## 阶段出口

- `groom` 能输出软提示。

## Split Ledger

- 2026-07-02：未拆分；原 exit：不变。
""",
    )

    health = _long_work_health(load_all(), load_projects()[0])

    assert health == {"candidates": 1, "hints": []}


def test_long_work_health_flags_split_without_exit_delta(repo):
    _make("长期控制面")
    _set_body(
        "ISSUE-1",
        """## 当前状态卡

- 当前阶段：P1 health hint。
- 下一步最小动作：跑一次 groom。
- 进入实现闸门：不改 schema。
- 不做什么：不做大型 TUI。

## 阶段出口

- 收口 P1。

## Split Ledger

- 2026-07-02：拆出一个子任务。
""",
    )

    health = _long_work_health(load_all(), load_projects()[0])

    assert health["hints"] == [{"id": "ISSUE-1", "hints": ["split 缺出口变化"]}]


def test_health_json_shape(repo, capsys):
    _make("umbrella")
    _make("child one", parent="ISSUE-1")
    _make("child two", parent="ISSUE-1")

    capsys.readouterr()
    C.cmd_health(as_json=True)
    data = json.loads(capsys.readouterr().out)

    assert set(data) == {"schema_version", "source", "scope", "summary", "signals"}
    assert data["schema_version"] == 1
    assert data["source"] == "docket.work_health"
    assert data["scope"] == {"project": ""}
    assert data["summary"] == {"long_work_candidates": 1, "signal_count": 4}
    assert data["signals"][0]["kind"] == "missing_status_card"
    assert data["signals"][0]["recommended_action"]


def test_groom_footer_shows_long_work_health(repo, capsys):
    _make("umbrella")
    _make("child one", parent="ISSUE-1")
    _make("child two", parent="ISSUE-1")
    capsys.readouterr()
    C.cmd_groom(today_str="2026-06-25")
    out = capsys.readouterr().out
    assert "长期工作健康: 候选 1 条 · 结构提示 1 条" in out
    assert "长期工作提示: ISSUE-1(" in out
    assert "缺当前状态卡" in out
