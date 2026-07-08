from __future__ import annotations

import json

import pytest

from docket import commands as C
from docket.errors import DocketError
from docket.issue import load_by_id, parse_issue


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    monkeypatch.setenv("DOCKET_WORKTREE_CLOSE_GATE", "0")
    return tmp_path


def test_new_stamps_uid_project_iid_aliases_and_resolves_display(repo):
    C.cmd_project_new("work", title="Work", prefix="WORK")
    C.cmd_new("identity", "work", "High", None, "", "", "")

    issue = load_by_id("WORK-1")
    assert issue.id() == "ISSUE-1"
    assert issue.uid().startswith("dkt_")
    assert issue.project_iid() == 1
    assert "ISSUE-1" in issue.aliases()
    assert "WORK-1" in issue.aliases()
    assert load_by_id(issue.uid()).id() == "ISSUE-1"


def test_project_change_preserves_old_alias_and_assigns_new_project_iid(repo):
    C.cmd_project_new("work", title="Work", prefix="WORK")
    C.cmd_project_new("ops", title="Ops", prefix="OPS")
    C.cmd_new("first work", "work", "No priority", None, "", "", "")
    C.cmd_new("first ops", "ops", "No priority", None, "", "", "")

    C.cmd_set("WORK-1", project="ops")

    moved = load_by_id("OPS-2")
    assert moved.id() == "ISSUE-1"
    assert moved.project() == "ops"
    assert moved.project_iid() == 2
    assert load_by_id("WORK-1").id() == "ISSUE-1"
    assert "WORK-1" in moved.aliases()
    assert "OPS-2" in moved.aliases()


def test_bare_number_is_ambiguous_when_project_iids_collide(repo):
    C.cmd_project_new("work", title="Work", prefix="WORK")
    C.cmd_project_new("ops", title="Ops", prefix="OPS")
    C.cmd_new("first work", "work", "No priority", None, "", "", "")
    C.cmd_new("first ops", "ops", "No priority", None, "", "", "")

    with pytest.raises(DocketError) as ei:
        load_by_id("1")

    assert "ambiguous issue ref" in ei.value.message
    assert "WORK-1" in ei.value.message
    assert "OPS-1" in ei.value.message


def test_migrate_identity_stamps_legacy_issues(repo, capsys):
    (repo / "projects").mkdir()
    (repo / "projects" / "work.md").write_text(
        '---\nkey: work\ntitle: "Work"\nprefix: "WORK"\nstatus: active\n---\n',
        encoding="utf-8",
    )
    (repo / "issues" / "ISSUE-1.md").write_text(
        "---\n"
        "domain: pm\n"
        "id: ISSUE-1\n"
        'title: "legacy"\n'
        'description: "legacy"\n'
        "keywords: [pm]\n"
        "verified: 2026-07-08\n"
        "status: Todo\n"
        "state_type: unstarted\n"
        "priority: No priority\n"
        'project: "work"\n'
        "parent: ~\n"
        "created: 2026-07-08\n"
        "updated: 2026-07-08\n"
        "labels: []\n"
        "---\n\n"
        "legacy body\n",
        encoding="utf-8",
    )

    C.cmd_migrate_identity()

    out = capsys.readouterr().out
    assert "updated 1 issue(s)" in out
    issue = parse_issue(str(repo / "issues" / "ISSUE-1.md"))
    assert issue.uid().startswith("dkt_")
    assert issue.project_iid() == 1
    assert "ISSUE-1" in issue.aliases()
    assert "WORK-1" in issue.aliases()
    C.cmd_validate()


def test_resolve_falls_back_to_configured_tiers(repo, tmp_path, monkeypatch, capsys):
    personal = tmp_path / "personal"
    _write_tiers(tmp_path, monkeypatch, work=repo, personal=personal)
    _add_issue(
        personal,
        monkeypatch,
        project="rdp",
        prefix="RDP",
        aliases=["OPS-657"],
    )
    monkeypatch.setenv("DOCKET_ROOT", str(repo))
    capsys.readouterr()

    C.cmd_resolve("OPS-657", as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["display_ref"] == "RDP-1"
    assert payload["tier"] == "personal"
    assert payload["root"] == str(personal.resolve())


def test_resolve_reports_ambiguous_configured_tier_matches(repo, tmp_path, monkeypatch):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_tiers(tmp_path, monkeypatch, a=root_a, b=root_b)
    _add_issue(root_a, monkeypatch, project="ops", prefix="OPS", aliases=["SHARED-1"])
    _add_issue(root_b, monkeypatch, project="lab", prefix="LAB", aliases=["SHARED-1"])
    monkeypatch.setenv("DOCKET_ROOT", str(repo))

    with pytest.raises(DocketError) as ei:
        C.cmd_resolve("SHARED-1")

    assert "ambiguous issue ref across tiers" in ei.value.message
    assert "a:OPS-1" in ei.value.message
    assert "b:LAB-1" in ei.value.message


def _write_tiers(tmp_path, monkeypatch, **tiers):
    home = tmp_path / "home"
    config = home / ".config" / "docket"
    config.mkdir(parents=True)
    lines = ["[tiers]"]
    for name, root in tiers.items():
        lines.append(f'{name} = "{root}"')
    (config / "tiers.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))


def _add_issue(root, monkeypatch, *, project, prefix, aliases=None):
    (root / "issues").mkdir(parents=True)
    monkeypatch.setenv("DOCKET_ROOT", str(root))
    C.cmd_project_new(project, title=project.title(), prefix=prefix)
    C.cmd_new("issue", project, "No priority", None, "", "", "")
    issue = load_by_id(f"{prefix}-1")
    if aliases:
        issue.set_aliases([*issue.aliases(), *aliases])
        issue.write()
