"""Issue-owned artifact repositories.

Artifacts hold large handoff, requirement-bundle, or evidence payloads in a
sibling directory of their docket issue's PM data repo. This keeps payloads in
the same tier while ensuring they never live inside the PM Git repository.
Each artifact directory is its own Git repository at
``<DOCKET_ROOT>-artifacts/<ISSUE-ID>/``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import DocketError
from .issue import find_repo_root, load_all, load_by_id, normalize_id

SUPPORTED_TEMPLATES = ("handoff", "requirement")


@dataclass(frozen=True)
class ArtifactSummary:
    """Small display model for one issue artifact repository."""

    id: str
    path: Path
    exists: bool
    is_repo: bool
    head: str
    dirty: bool


def artifact_root() -> Path:
    """Return the external artifact directory for the resolved docket root."""
    docket_root = Path(find_repo_root())
    return docket_root.with_name(f"{docket_root.name}-artifacts")


def artifact_path(id_: str) -> Path:
    """Return the canonical artifact path for an issue id."""
    id_ = _existing_issue_id(id_)
    return artifact_root() / id_


def cmd_artifact_path(id_: str) -> None:
    """Print the canonical artifact path for one issue."""
    print(artifact_path(id_))


def cmd_artifact_init(id_: str, template: str = "handoff") -> None:
    """Create an issue-owned artifact Git repository from a named template."""
    template = (template or "").strip()
    if template not in SUPPORTED_TEMPLATES:
        allowed = ", ".join(SUPPORTED_TEMPLATES)
        raise DocketError(f'unknown artifact template "{template}" (want {allowed})')
    issue = load_by_id(id_)
    id_ = issue.id()
    root = artifact_root()
    path = root / id_
    root.mkdir(parents=True, exist_ok=True)
    if path.exists() and any(path.iterdir()):
        raise DocketError(f"artifact already exists: {path}")

    if template == "requirement":
        _init_requirement(path, id_, issue.title())
    else:
        _init_handoff(path, id_, issue.title())
    _ensure_git_repo(path)
    _commit_artifact(path, f"artifact({id_}): init {template}")
    print(f"artifact {id_}: initialized {path}")


def cmd_artifact_list(id_: str | None = None) -> None:
    """List artifact repositories, or summarize one issue's artifact."""
    if id_:
        summary = artifact_summary(id_)
        _print_summary(summary)
        return
    root = artifact_root()
    if not root.is_dir():
        print("(no artifacts)")
        return
    summaries = [
        artifact_summary(p.name)
        for p in sorted(root.iterdir())
        if p.is_dir() and not p.name.startswith(".")
    ]
    if not summaries:
        print("(no artifacts)")
        return
    for summary in summaries:
        _print_summary(summary)


def cmd_artifact_show(id_: str) -> None:
    """Show one artifact repository's location and Git status."""
    _print_summary(artifact_summary(id_))


def cmd_artifact_sync(id_: str | None = None, all_: bool = False) -> None:
    """Commit dirty artifact repositories for one issue or for all issues."""
    if all_:
        summaries = sync_all_artifacts()
        _print_sync_result(summaries)
        return
    if not id_:
        raise DocketError("artifact sync requires <id> or --all")
    summary = sync_artifact(id_)
    _print_sync_result([summary])


def artifact_summary(id_: str) -> ArtifactSummary:
    """Return a display-ready summary for one issue artifact."""
    id_ = _existing_issue_id(id_)
    path = artifact_root() / id_
    exists = path.exists()
    is_repo = (path / ".git").exists()
    head = _git_head(path) if is_repo else ""
    dirty = _git_dirty(path) if is_repo else False
    return ArtifactSummary(id_, path, exists, is_repo, head, dirty)


def artifact_show_line(id_: str) -> str | None:
    """Return the compact `docket show` artifact line for an issue, if present."""
    summary = artifact_summary(id_)
    if not summary.exists:
        return None
    state = _summary_state(summary)
    return f"{summary.path} ({state})"


def sync_artifact(id_: str) -> ArtifactSummary:
    """Commit dirty files in one artifact repository and return its new summary."""
    summary = artifact_summary(id_)
    if not summary.exists:
        raise DocketError(f"artifact not found: {summary.path}")
    if not summary.is_repo:
        raise DocketError(f"artifact is not a git repo: {summary.path}")
    if summary.dirty:
        _commit_artifact(summary.path, f"artifact({summary.id}): sync")
    return artifact_summary(summary.id)


def sync_all_artifacts() -> list[ArtifactSummary]:
    """Commit dirty files in every initialized artifact repository.

    Returns only repositories that actually had changes to commit.
    """
    root = artifact_root()
    if not root.is_dir():
        return []
    synced = []
    issue_ids = {is_.id() for is_ in load_all()}
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith(".") or path.name not in issue_ids:
            continue
        if not (path / ".git").exists():
            continue
        summary = artifact_summary(path.name)
        if not summary.dirty:
            continue
        _commit_artifact(path, f"artifact({summary.id}): sync")
        synced.append(artifact_summary(path.name))
    return synced


def _existing_issue_id(id_: str) -> str:
    """Normalize an id and verify that the issue exists."""
    normalized = normalize_id(id_)
    load_by_id(normalized)
    return normalized


def _init_handoff(path: Path, id_: str, title: str) -> None:
    """Create the minimal handoff template."""
    path.mkdir(parents=True, exist_ok=True)
    files = {
        "brief.md": f"# {id_} brief\n\n- Title: {title}\n- Source of truth: docket {id_}\n",
        "progress.md": f"# {id_} progress\n\n## Current\n\n## Next\n",
        "review.md": f"# {id_} review\n\n## Findings\n\n## Decisions\n",
        "evidence.md": f"# {id_} evidence\n\n## Verification\n\n",
    }
    for rel, content in files.items():
        (path / rel).write_text(content, encoding="utf-8")


def _init_requirement(path: Path, id_: str, title: str) -> None:
    """Create an RDP requirement bundle using the rd-pipeline CLI."""
    if shutil.which("rd-pipeline") is None:
        raise DocketError(
            "rd-pipeline not found on PATH; install it or use --template handoff"
        )
    if path.exists():
        path.rmdir()
    cmd = [
        "rd-pipeline",
        "init",
        str(path),
        "--pm-id",
        id_,
        "--slug",
        id_.lower(),
        "--title",
        title,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise DocketError(f"rd-pipeline init failed: {detail}")


def _ensure_git_repo(path: Path) -> None:
    """Initialize the artifact directory as a Git repository if needed."""
    if (path / ".git").exists():
        return
    proc = subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DocketError(f"git init artifact: {proc.stderr.strip()}")


def _commit_artifact(path: Path, message: str) -> None:
    """Commit all changes inside one artifact repository."""
    add = subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True, text=True)
    if add.returncode != 0:
        raise DocketError(f"git add artifact: {add.stderr.strip()}")
    if not _git_dirty(path, include_untracked=False):
        return
    cmd = [
        "git",
        "-c",
        "user.name=docket",
        "-c",
        "user.email=docket@example.invalid",
        "commit",
        "-m",
        message,
    ]
    commit = subprocess.run(cmd, cwd=path, capture_output=True, text=True)
    if commit.returncode != 0:
        raise DocketError(f"git commit artifact: {commit.stderr.strip()}")


def _git_head(path: Path) -> str:
    """Return the short HEAD hash, or an empty string before the first commit."""
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _git_dirty(path: Path, *, include_untracked: bool = True) -> bool:
    """Return whether a Git repository has working tree or index changes."""
    args = ["git", "status", "--porcelain"]
    if not include_untracked:
        args.append("--untracked-files=no")
    proc = subprocess.run(args, cwd=path, capture_output=True, text=True)
    return proc.returncode == 0 and proc.stdout.strip() != ""


def _summary_state(summary: ArtifactSummary) -> str:
    """Render a compact state string for an artifact summary."""
    if not summary.exists:
        return "missing"
    if not summary.is_repo:
        return "not-git"
    bits = []
    if summary.head:
        bits.append(summary.head)
    else:
        bits.append("no-commit")
    bits.append("dirty" if summary.dirty else "clean")
    return " · ".join(bits)


def _print_summary(summary: ArtifactSummary) -> None:
    """Print a single artifact summary line."""
    print(f"{summary.id}  {_summary_state(summary)}  {summary.path}")


def _print_sync_result(summaries: list[ArtifactSummary]) -> None:
    """Print the result of an artifact sync command."""
    if not summaries:
        print("artifact sync: 无 artifact repo 待同步")
        return
    for summary in summaries:
        print(f"artifact sync: {summary.id} {_summary_state(summary)}")
