from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from docket import artifact as A
from docket import commands as C
from docket.gitops import cmd_sync


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "issues").mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    return tmp_path


def _new_issue(title: str = "artifact issue") -> None:
    C.cmd_new(title, "", "No priority", None, "", "", "", status=None)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _artifact_dir(repo: Path) -> Path:
    return repo.with_name(f"{repo.name}-artifacts")


def test_artifact_path_follows_current_docket_root(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    for root in (root_a, root_b):
        (root / "issues").mkdir(parents=True)
        monkeypatch.setenv("DOCKET_ROOT", str(root))
        _new_issue()

    monkeypatch.setenv("DOCKET_ROOT", str(root_a))
    assert A.artifact_path("ISSUE-1") == tmp_path / "a-artifacts" / "ISSUE-1"

    monkeypatch.setenv("DOCKET_ROOT", str(root_b))
    assert A.artifact_path("ISSUE-1") == tmp_path / "b-artifacts" / "ISSUE-1"


def test_artifact_init_creates_handoff_repo_outside_pm_repo(repo, capsys):
    _git(repo, "init")
    _new_issue()
    capsys.readouterr()

    A.cmd_artifact_init("ISSUE-1", "handoff")
    out = capsys.readouterr().out
    artifact = _artifact_dir(repo) / "ISSUE-1"

    assert "initialized" in out
    assert (artifact / ".git").is_dir()
    assert (artifact / "brief.md").is_file()
    assert not (repo / "artifacts").exists()
    status = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    assert "artifacts" not in status
    assert "brief.md" not in status


def test_artifact_sync_commits_direct_checkout_edits(repo, capsys):
    _new_issue()
    capsys.readouterr()
    A.cmd_artifact_init("ISSUE-1", "handoff")
    artifact = _artifact_dir(repo) / "ISSUE-1"
    before = _git(artifact, "rev-list", "--count", "HEAD").stdout.strip()

    (artifact / "progress.md").write_text("# changed\n", encoding="utf-8")
    A.cmd_artifact_sync("ISSUE-1")

    after = _git(artifact, "rev-list", "--count", "HEAD").stdout.strip()
    assert int(after) == int(before) + 1
    assert _git(artifact, "status", "--porcelain").stdout == ""


def test_artifact_list_skips_orphaned_artifact_dirs(repo, capsys):
    _new_issue()
    capsys.readouterr()
    A.cmd_artifact_init("ISSUE-1", "handoff")
    capsys.readouterr()

    orphan = _artifact_dir(repo) / "ISSUE-999"
    orphan.mkdir(parents=True)
    (orphan / ".git").mkdir()

    A.cmd_artifact_list()

    out = capsys.readouterr().out
    assert "ISSUE-1" in out
    assert "ISSUE-999" not in out


def test_docket_sync_reports_only_dirty_artifact_repos(repo, capsys):
    _new_issue()
    capsys.readouterr()
    A.cmd_artifact_init("ISSUE-1", "handoff")
    artifact = _artifact_dir(repo) / "ISSUE-1"
    capsys.readouterr()

    cmd_sync()
    assert capsys.readouterr().out.strip() == "sync: 无外部直写待收编"

    (artifact / "progress.md").write_text("# changed\n", encoding="utf-8")
    cmd_sync()

    out = capsys.readouterr().out
    assert "sync: 同步 1 个 artifact repo → ISSUE-1" in out
    assert _git(artifact, "status", "--porcelain").stdout == ""


def test_show_prints_artifact_summary(repo, capsys):
    _new_issue()
    capsys.readouterr()
    A.cmd_artifact_init("ISSUE-1", "handoff")
    capsys.readouterr()

    C.cmd_show("ISSUE-1", no_comments=True)
    out = capsys.readouterr().out

    assert "artifact:" in out
    assert str(_artifact_dir(repo) / "ISSUE-1") in out


def test_requirement_template_delegates_to_rd_pipeline(
    repo, tmp_path, monkeypatch, capsys
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rd_pipeline = fake_bin / "rd-pipeline"
    rd_pipeline.write_text(
        """#!/bin/sh
set -eu
test "$1" = "init"
target="$2"
mkdir -p "$target/capture" "$target/design" "$target/evidence"
cat > "$target/requirement.yaml" <<EOF
pm_id: "$4"
slug: "$6"
title: "$8"
EOF
touch "$target/capture/README.md"
touch "$target/design/spec.md"
touch "$target/acceptance.md"
""",
        encoding="utf-8",
    )
    rd_pipeline.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    _new_issue("requirement title")
    capsys.readouterr()

    A.cmd_artifact_init("ISSUE-1", "requirement")
    artifact = _artifact_dir(repo) / "ISSUE-1"

    assert (artifact / ".git").is_dir()
    assert (
        (artifact / "requirement.yaml")
        .read_text(encoding="utf-8")
        .startswith('pm_id: "ISSUE-1"')
    )
