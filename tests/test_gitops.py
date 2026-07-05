from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from docket.gitops import catch_up_stray_writes


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("issues", "comments", "projects"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("DOCKET_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCKET_ID_PREFIX", raising=False)

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Docket Test")
    _git(tmp_path, "config", "user.email", "docket-test@example.invalid")

    (tmp_path / "issues" / "ISSUE-1.md").write_text("issue v1\n", encoding="utf-8")
    (tmp_path / "comments" / "ISSUE-1.md").write_text("comment v1\n", encoding="utf-8")
    (tmp_path / "projects" / "pm.md").write_text("project v1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


def test_sync_sweeps_project_markdown_stray_writes(git_repo: Path) -> None:
    (git_repo / "issues" / "ISSUE-1.md").write_text("issue v2\n", encoding="utf-8")
    (git_repo / "comments" / "ISSUE-1.md").write_text("comment v2\n", encoding="utf-8")
    (git_repo / "projects" / "pm.md").write_text("project v2\n", encoding="utf-8")

    swept = catch_up_stray_writes()

    assert sorted(swept) == ["ISSUE-1", "ISSUE-1", "pm"]
    assert _git(git_repo, "status", "--porcelain") == ""
    project_log = _git(
        git_repo, "log", "--format=%s", "-1", "--", "projects/pm.md"
    ).strip()
    assert project_log == "pm(sync): 收编外部直写 pm"
