"""Terminal rendering: CJK/ANSI-aware width, padding, tables, color, progress bars.

Port of color.go + the table helpers in commands.go / projects.go. The Go code
measured width with mattn/go-runewidth (default config: ambiguous-width = 1);
here `unicodedata.east_asian_width` reproduces that — only Wide/Fullwidth count
as 2, combining marks as 0, everything else (incl. the box/symbol glyphs
✓●○▸█░·— used in views, all East-Asian "ambiguous") as 1.
"""

import os
import re
import unicodedata

# ---- color ----

_color_enabled = False

C_RESET = "\x1b[0m"
C_BOLD = "\x1b[1m"
C_DIM = "\x1b[2m"
C_GREEN = "\x1b[32m"
C_YELLOW = "\x1b[33m"
C_CYAN = "\x1b[36m"
C_GRAY = "\x1b[90m"


def init_color(is_tty: bool) -> None:
    """Decide color once from the REAL terminal, before stdout is redirected to
    the telemetry tee. Auto: on when stdout is a TTY. DOCKET_COLOR=1/0 overrides;
    NO_COLOR forces off. Agents (piped, non-TTY) get plain output automatically."""
    global _color_enabled
    v = os.environ.get("DOCKET_COLOR", "")
    if v in ("1", "always", "true"):
        _color_enabled = True
        return
    if v in ("0", "never", "false"):
        _color_enabled = False
        return
    if os.environ.get("NO_COLOR"):
        return
    if is_tty:
        _color_enabled = True


def colorize(code: str, s: str) -> str:
    """Wrap s in an ANSI code (no-op when color is off or code is empty)."""
    if not _color_enabled or code == "":
        return s
    return code + s + C_RESET


def status_color(status: str) -> str:
    return {
        "In Progress": C_CYAN,
        "Done": C_GREEN,
        "Todo": C_YELLOW,
        "Backlog": C_GRAY,
        "Canceled": C_GRAY,
    }.get(status, "")


# ---- width ----

_ANSI_RE = re.compile("\x1b\\[[0-9;]*m")


def _char_width(c: str) -> int:
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def display_width(s: str) -> int:
    """Terminal columns, ignoring ANSI color codes so colored cells still align."""
    return sum(_char_width(c) for c in _ANSI_RE.sub("", s))


# ---- tables ----


def pad(s: str, w: int) -> str:
    dw = display_width(s)
    if dw >= w:
        return s
    return s + " " * (w - dw)


def print_table(rows, headers) -> None:
    cols = len(headers)
    widths = [display_width(h) for h in headers]
    for r in rows:
        for i in range(min(cols, len(r))):
            w = display_width(r[i])
            widths[i] = max(widths[i], w)
    lines = []
    header = ""
    for i, h in enumerate(headers):
        header += pad(h, widths[i])
        if i < cols - 1:
            header += "  "
    lines.append(header)
    sep = ""
    for i in range(cols):
        sep += "-" * widths[i]
        if i < cols - 1:
            sep += "  "
    lines.append(sep)
    for r in rows:
        line = ""
        for i in range(cols):
            cell = r[i] if i < len(r) else ""
            line += pad(cell, widths[i])
            if i < cols - 1:
                line += "  "
        lines.append(line)
    print("\n".join(lines))


def clip_runes(s: str, n: int) -> str:
    """Truncate s to at most n code points, appending "…" if clipped."""
    if len(s) <= n:
        return s
    return s[:n] + "…"


def progress_bar(done: int, total: int, width: int) -> str:
    """Render a done/total ratio as a width-char bar. A non-zero numerator always
    shows at least one filled cell."""
    if total <= 0:
        return colorize(C_GRAY, "·" * width)
    filled = done * width // total
    if done > 0 and filled == 0:
        filled = 1
    filled = min(filled, width)
    return colorize(C_GREEN, "█" * filled) + colorize(C_GRAY, "░" * (width - filled))
