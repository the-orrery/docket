from __future__ import annotations

import pytest

from docket import commands as C
from docket import projects as P
from docket.issue import load_all, quote_scalar


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "kb.md").write_text(
        """---
domain: pm
key: kb
title: "知识库"
prefix: KB
status: active
created: 2026-06-13
updated: 2026-06-13
---

# 知识库 (KB)

项目计划正文。
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    # hermetic: don't inherit DOCKET_ID_PREFIX from the dev's env.
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


def _make(title: str, status: str):
    C.cmd_new(title, "kb", "No priority", None, "", "", "", status=status)


def _issue_by_title(title: str):
    for is_ in load_all():
        if is_.title() == title:
            return is_
    raise AssertionError(f"issue not found: {title}")


def _future():
    from docket.issue import cn_now

    return (cn_now().replace(year=cn_now().year + 1)).strftime("%Y-%m-%d")


def test_project_progress_excludes_canceled_from_denominator(repo, capsys):
    _make("done one", "Done")
    _make("done two", "Done")
    _make("cut from scope", "Canceled")
    capsys.readouterr()

    P.cmd_projects(False)
    out = capsys.readouterr().out
    assert "DONE/SCOPE" in out
    assert "2/2" in out
    assert "2/3" not in out

    P.cmd_overview()
    out = capsys.readouterr().out
    assert "2/2" in out
    assert "2/3" not in out

    P.cmd_project("kb")
    out = capsys.readouterr().out
    assert "2/2" in out
    assert "2/3" not in out
    assert "已取消 · 1" in out


def test_projects_counts_include_future_wake_open_issue(repo, capsys):
    _make("sleepy scope", "Todo")
    C.cmd_set("ISSUE-1", wake=_future())
    capsys.readouterr()

    P.cmd_projects(False)
    out = capsys.readouterr().out
    assert "0/1" in out and "0/0" not in out


def test_overview_hides_future_wake_from_focus_but_keeps_project_scope(repo, capsys):
    _make("sleepy active", "In Progress")
    C.cmd_set("ISSUE-1", wake=_future())
    capsys.readouterr()

    P.cmd_overview()
    out = capsys.readouterr().out
    focus = out.split("\n本批", 1)[0]
    project_section = out.split("\n项目\n", 1)[1]
    assert "sleepy active" not in focus
    assert "0/1" in project_section and "0/0" not in project_section


def test_projects_table_orders_by_activity(repo, capsys):
    # second project 'zeta' (字母序在 kb 之后) with 2 active issues; kb 只有 1 个已完成。
    (repo / "projects" / "zeta.md").write_text(
        '---\nkey: zeta\ntitle: "Zeta"\nprefix: ZETA\nstatus: active\n---\n',
        encoding="utf-8",
    )
    _make("kb done", "Done")  # kb: 0 active
    C.cmd_new("z one", "zeta", "No priority", None, "", "", "", status="In Progress")
    C.cmd_new("z two", "zeta", "No priority", None, "", "", "", status="In Progress")
    capsys.readouterr()

    P.cmd_projects(False)
    out = capsys.readouterr().out
    # 活跃度优先:zeta(2 active) 排在 kb(0 active) 前,尽管 'kb' < 'zeta' 字母序。
    assert out.index("zeta") < out.index("kb")


def test_projects_table_orders_work_lane_before_non_work(repo, capsys):
    (repo / "projects" / "alpha.md").write_text(
        '---\nkey: alpha\ntitle: "Alpha Work"\nprefix: ALPHA\nlane: work\nstatus: active\n---\n',
        encoding="utf-8",
    )
    _make("kb active", "In Progress")  # non-work: 1 active; alpha/work: 0 active
    capsys.readouterr()

    P.cmd_projects(False)
    out = capsys.readouterr().out
    assert "工作项目" in out
    assert "非工作项目" in out
    assert out.index("alpha") < out.index("kb")
    assert out.index("work") < out.index("non-work")

    P.cmd_overview()
    out = capsys.readouterr().out
    project_section = out.split("\n项目\n", 1)[1]
    assert project_section.index("工作项目") < project_section.index("非工作项目")
    assert project_section.index("alpha") < project_section.index("kb")


def test_projects_table_uses_rank_before_activity(repo, capsys):
    (repo / "projects" / "alpha.md").write_text(
        '---\nkey: alpha\ntitle: "Alpha"\nprefix: ALPHA\nlane: work\nrank: 20\nstatus: active\n---\n',
        encoding="utf-8",
    )
    (repo / "projects" / "beta.md").write_text(
        '---\nkey: beta\ntitle: "Beta"\nprefix: BETA\nlane: work\nrank: 10\nstatus: active\n---\n',
        encoding="utf-8",
    )
    C.cmd_new(
        "alpha active", "alpha", "No priority", None, "", "", "", status="In Progress"
    )
    C.cmd_new("alpha todo", "alpha", "No priority", None, "", "", "", status="Todo")
    C.cmd_new(
        "beta active", "beta", "No priority", None, "", "", "", status="In Progress"
    )
    capsys.readouterr()

    P.cmd_projects(False)
    out = capsys.readouterr().out
    assert out.index("beta") < out.index("alpha")

    P.cmd_overview()
    out = capsys.readouterr().out
    project_section = out.split("\n项目\n", 1)[1]
    assert project_section.index("beta") < project_section.index("alpha")


def test_projects_warning_splits_unassigned_and_unknown_project(repo, capsys):
    _make("normal project", "Todo")
    C.cmd_new("missing project", "", "No priority", None, "", "", "")
    C.cmd_new("unknown project", "kb", "No priority", None, "", "", "")
    unknown = _issue_by_title("unknown project")
    unknown.set("project", quote_scalar("ghost"))
    unknown.write()
    missing = _issue_by_title("missing project")
    capsys.readouterr()

    P.cmd_projects(False)
    out = capsys.readouterr().out

    assert "kb" in out
    assert "⚠ 1 issue(s) have no project" in out
    assert missing.id() in out
    assert "⚠ 1 issue(s) reference unknown project key" in out
    assert f"{unknown.id()} -> ghost" in out
    assert "have a project with no projects/<key>.md" not in out


# ---- project registration (project new + new --project foreign-key check) ----


def test_project_new_writes_registered_file(repo, capsys):
    C.cmd_project_new("acme", title="Acme Demo", prefix="ACME")
    capsys.readouterr()
    p = repo / "projects" / "acme.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "key: acme" in text
    assert "lane: non-work" in text
    assert "prefix: ACME" in text
    assert 'title: "Acme Demo"' in text
    by_key, _ = P.load_projects()
    assert by_key["acme"].prefix == "ACME"
    assert by_key["acme"].lane == "non-work"


def test_project_new_can_write_rank(repo, capsys):
    C.cmd_project_new("acme", title="Acme Demo", prefix="ACME", rank="30")
    capsys.readouterr()
    p = repo / "projects" / "acme.md"
    text = p.read_text(encoding="utf-8")
    assert "rank: 30" in text
    by_key, _ = P.load_projects()
    assert by_key["acme"].rank == "30"


def test_project_new_defaults_prefix_and_title_from_key(repo):
    C.cmd_project_new("alpha")
    by_key, _ = P.load_projects()
    assert by_key["alpha"].prefix == "ALPHA"
    assert by_key["alpha"].title == "alpha"


def test_project_new_rejects_duplicate(repo):
    with pytest.raises(C.DocketError):
        C.cmd_project_new("kb")  # already registered by the fixture


def test_new_rejects_unregistered_project(repo):
    with pytest.raises(C.DocketError):
        C.cmd_new("orphan", "ghost", "No priority", None, "", "", "")


def test_new_accepts_registered_project(repo, capsys):
    C.cmd_new("ok", "kb", "No priority", None, "", "", "")
    assert "created" in capsys.readouterr().out


def test_new_with_new_project_flag_registers_then_creates(repo, capsys):
    C.cmd_new("first", "acme", "No priority", None, "", "", "", new_project=True)
    capsys.readouterr()
    by_key, _ = P.load_projects()
    assert "acme" in by_key  # registered on the fly
    issues = list((repo / "issues").glob("*.md"))
    assert any('project: "acme"' in f.read_text(encoding="utf-8") for f in issues)


# ---- CLI wiring: `projects new` is a real subcommand, not a magic-string dispatch ----


def test_projects_new_is_a_real_subcommand(repo):
    """Locks the interface the old magic `project new` dispatch lacked a test for:
    `projects` still lists, `projects new <key>` registers via a genuine
    subcommand, `project <key>` still drills, and a project keyed "new" is no
    longer a reserved-word casualty of the dispatch hack."""
    from typer.testing import CliRunner

    from docket.cli import app

    runner = CliRunner()
    assert runner.invoke(app, ["projects"]).exit_code == 0  # bare list
    assert runner.invoke(app, ["projects", "--all"]).exit_code == 0

    r = runner.invoke(app, ["projects", "new", "acme", "--prefix", "ACME"])
    assert r.exit_code == 0, r.output
    by_key, _ = P.load_projects()
    assert by_key["acme"].prefix == "ACME"

    assert runner.invoke(app, ["project", "kb"]).exit_code == 0  # singular drill

    # "new" is now a perfectly valid project key (no magic dispatch to collide).
    assert runner.invoke(app, ["projects", "new", "new"]).exit_code == 0
    by_key, _ = P.load_projects()
    assert "new" in by_key
