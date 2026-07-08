"""Per-write git auto-commit + atomic write + history. Port of git.go.

Every docket write records ONE commit (the data repo carries its own change
history; `docket history <id>` = git log of that file). Concurrency safety is
mechanical, not lock-based: commit ONLY the written file (git pathspec mode) so
concurrent writers to other files are never captured, never `add -A`, never
amend; files are written atomically (tmp + rename). A rare index/ref-lock
collision fails cleanly and a short backoff retry almost always wins.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from .errors import DocketError
from .fsops import atomic_write_file as _atomic_write_file
from .issue import find_repo_root, load_by_id

# Backoff for transient git lock contention (index.lock / ref lock) when
# concurrent docket writers commit at once.
_GIT_RETRY_DELAYS = [0.0, 0.15, 0.30, 0.60, 1.20]


def atomic_write_file(path: str, data: str, perm: int = 0o644) -> None:
    """Write a file atomically; kept here for existing gitops import callers."""
    _atomic_write_file(path, data, perm)


def _is_git_repo(root: str) -> bool:
    p = Path(root) / ".git"
    return p.is_dir() or p.is_file()


def _is_lock_contention(out: bytes) -> bool:
    s = out.decode("utf-8", "replace")
    return ".lock" in s or "Unable to create" in s or "another git process" in s


def git_run(root: str, *args: str):
    """Run `git <args>` in root, retrying only on transient lock contention.
    Returns None on success, else an error message string."""
    last = None
    for d in _GIT_RETRY_DELAYS:
        if d > 0:
            time.sleep(d)
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True)
        out = proc.stdout + proc.stderr  # CombinedOutput
        if proc.returncode == 0:
            return None
        last = "git {}: {}".format(
            " ".join(args), out.decode("utf-8", "replace").strip()
        )
        if not _is_lock_contention(out):
            return last  # a real error — do not retry
    return last


def _git_path_clean(root: str, rel: str):
    """Report whether rel has no staged/working changes."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", rel], cwd=root, capture_output=True
    )
    if proc.returncode != 0:
        return False, "git status failed"
    return proc.stdout.decode("utf-8", "replace").strip() == "", None


def _warn_commit(rel: str, err: str) -> None:
    print(
        f"⚠️  写入成功但 auto-commit 失败({err});该变更未记入历史,"
        f"可手动 git commit -- {rel}",
        file=sys.stderr,
    )


def auto_commit(abs_path: str, msg: str) -> None:
    """Record a single data-file change as one commit. Best-effort and non-fatal:
    the file write already succeeded, so a commit failure warns rather than
    failing a change the user already made."""
    try:
        root = find_repo_root()
    except DocketError:
        return
    if not _is_git_repo(root):
        return  # not a git repo (e.g. a tmp test root) — nothing to record
    rel = os.path.relpath(abs_path, root)
    # Stage only this file: required for new files, harmless for tracked ones.
    err = git_run(root, "add", "--", rel)
    if err is not None:
        _warn_commit(rel, err)
        return
    # No actual change? skip (a no-op write must not error on empty commit).
    clean, _ = _git_path_clean(root, rel)
    if clean:
        return
    # Commit ONLY this path (pathspec = --only mode): never -A, never amend.
    err = git_run(root, "commit", "-m", msg, "--", rel)
    if err is not None:
        # A concurrent sibling may have committed our content between the
        # clean-check and here (TOCTOU). git's wording for that varies by version
        # and locale ("nothing to commit" / "no changes added to commit" / …), so
        # instead of matching the message we re-check: if rel is now clean the
        # change IS in history (ours or the sibling's) — success. Only warn if it
        # is still dirty (a genuine failure, e.g. exhausted lock-retry backoff).
        clean, _ = _git_path_clean(root, rel)
        if clean:
            return
        _warn_commit(rel, err)


def catch_up_stray_writes() -> list:
    """Commit any uncommitted issues/comments/projects md that bypassed docket (a direct
    Write/Edit, sed, another tool, or an old non-auto-committing docket), one
    single-file commit each. Returns the list of swept ids. Best-effort.

    This is NO LONGER run before every write — the per-write hot
    path only self-commits its own file, so concurrent writers no longer scan
    the whole tree and fight to commit each other's in-flight files. Stray sweep
    now happens via `docket sync` (run it from the SessionStart hook / on demand)."""
    swept: list = []
    try:
        root = find_repo_root()
    except DocketError:
        return swept
    if not _is_git_repo(root):
        return swept
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "issues", "comments", "projects"],
        cwd=root,
        capture_output=True,
    )
    if proc.returncode != 0:
        return swept
    out = proc.stdout.decode("utf-8", "replace")
    for line in out.rstrip("\n").split("\n"):
        # split() is robust to the leading space in porcelain XY codes (" M path")
        # and to multiple spaces; our paths never contain spaces, so the last
        # field is the path (a rename "old -> new" yields "new").
        fields = line.split()
        if len(fields) <= 1:
            continue
        rel = fields[-1]
        if not rel.endswith(".md"):
            continue
        id_ = Path(rel).stem
        auto_commit(str(Path(root) / rel), f"pm(sync): 收编外部直写 {id_}")
        swept.append(id_)
    return swept


def cmd_sync() -> None:
    """Sweep PM files and artifact repositories edited outside docket into history.

    PM data files are committed one file at a time in the PM repo. Artifact
    payloads live in external issue-owned Git repositories, so their dirty
    working trees are committed there instead of entering PM history.
    """
    from .artifact import sync_all_artifacts

    swept = catch_up_stray_writes()
    artifacts = sync_all_artifacts()
    if not swept and not artifacts:
        print("sync: 无外部直写待收编")
        return
    if swept:
        print(f"sync: 收编 {len(swept)} 个外部直写 → {', '.join(swept)}")
    if artifacts:
        ids = ", ".join(s.id for s in artifacts)
        print(f"sync: 同步 {len(artifacts)} 个 artifact repo → {ids}")


def cmd_history(id_: str) -> None:
    """Print the commit history of an issue (and its comments) — the audit trail
    produced by per-write auto-commits."""
    root = find_repo_root()
    id_ = load_by_id(id_).id()  # ensure the issue exists (raises if not)
    issue_rel = str(Path("issues") / (id_ + ".md"))
    comment_rel = str(Path("comments") / (id_ + ".md"))
    proc = subprocess.run(
        [
            "git",
            "log",
            "--date=format:%Y-%m-%d %H:%M",
            "--pretty=format:%h  %ad  %s",
            "--",
            issue_rel,
            comment_rel,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise DocketError(f"git log for {id_}: {proc.stderr.strip()}")
    print(proc.stdout, end="")
    print()


# Field/record separators for machine-parsing `git log` output: bytes that never
# occur in a commit hash / subject / body (US between fields, RS between commits).
_LOG_FS = "\x1f"
_LOG_RS = "\x1e"


def git_log_records(repo: str, limit: int) -> list[tuple[str, str, str]]:
    """Read the last `limit` commits of an ARBITRARY git `repo` (not the data
    repo) as (short_hash, subject, body) triples, newest first. Read-only.
    Raises DocketError if `repo` isn't a git repo or git fails — the caller
    decides how loud. Used by `docket orphans` to scan a code repo's commit
    messages for issue references."""
    fmt = f"%h{_LOG_FS}%s{_LOG_FS}%b{_LOG_RS}"
    proc = subprocess.run(
        ["git", "-C", repo, "log", f"-n{limit}", f"--pretty=format:{fmt}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "not a git repository?"
        raise DocketError(f"git log in {repo}: {detail}")
    records: list[tuple[str, str, str]] = []
    for rec in proc.stdout.split(_LOG_RS):
        rec = rec.strip("\n")
        if rec.strip() == "":
            continue
        parts = rec.split(_LOG_FS)
        h = parts[0] if len(parts) > 0 else ""
        subj = parts[1] if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""  # noqa: PLR2004
        records.append((h, subj, body))
    return records
