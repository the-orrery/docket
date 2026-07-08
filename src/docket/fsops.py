"""Filesystem primitives shared by docket modules."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_file(path: str, data: str, perm: int = 0o644) -> None:
    """Write a file via same-directory temp file and atomic rename."""
    d = str(Path(path).parent)
    fd, tmp = tempfile.mkstemp(prefix=".docket-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(data)
        Path(tmp).chmod(perm)
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise
