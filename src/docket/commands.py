"""Command implementations for the docket CLI.

Each cmd_* is called by the Typer layer in cli.py with already-parsed args (so
the flag-parsing switch in the Go originals is gone; Typer handles it). Output
is byte-for-byte parity with the Go CLI: every fmt.Printf/Println/Print maps to
a Python print with the same trailing-newline behaviour. Errors raise DocketError
(real fault) or ExitSignal (intentional non-zero, no fault — validate only).

Table-render helpers (pad/print_table/sort_by_priority/clip_runes/display_id)
live in render.py / issue.py / projects.py; only issue_rows is local here.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .artifact import artifact_show_line
from .errors import DocketError, ExitSignal
from .gitops import auto_commit, git_log_records
from .issue import (
    TRIAGE_TTL_DAYS,
    Issue,
    cn_now,
    find_repo_root,
    id_num,
    id_prefix,
    issues_dir,
    load_all,
    load_by_id,
    max_id,
    normalize_id,
    parse_batch,
    parse_bool,
    quote_scalar,
    sort_by_priority,
    today,
    unquote_scalar,
)
from .projects import display_id, load_projects
from .render import (
    C_BOLD,
    C_CYAN,
    C_DIM,
    clip_runes,
    colorize,
    pad,
    print_table,
    status_color,
)
from .states import (
    STATE_TYPE_TO_STATUS,
    resolve_priority,
    resolve_state,
    valid_priority,
    valid_state_type,
)
from .worktree_gate import ensure_worktrees_reconciled

# ---- dependency edges (blocked_by) ----

_CLOSED_STATES = ("completed", "canceled")


def open_blockers(is_, by_id):
    """Blockers not yet closed, as (id, Issue|None) pairs. A dangling ref counts
    as open (loud — validate flags it) rather than silently unblocking. A triage
    (un-accepted) blocker is NOT a valid blocker (ADR-008) — it must not silently
    hold a real issue out of `ready`."""
    out = []
    for bid in is_.blocked_by():
        b = by_id.get(bid)
        if b is not None and b.is_triage():
            continue
        if b is None or b.state_type() not in _CLOSED_STATES:
            out.append((bid, b))
    return out


def blocks_of(is_, issues):
    """Computed reverse edges: open issues whose blocked_by names this one."""
    return [
        o
        for o in issues
        if o.state_type() not in _CLOSED_STATES and is_.id() in o.blocked_by()
    ]


# ---- table rows ----


def issue_rows(issues, projects, by_id=None):
    """Table rows; with by_id, titles of dependency-blocked issues get a ⛔."""
    rows = []
    for is_ in issues:
        title = is_.title()
        if by_id is not None and open_blockers(is_, by_id):
            title = "⛔ " + title
        # mark un-accepted triage proposals so a bare `docket list` can't be
        # mistaken for a normal Todo and `start`ed by accident (ADR-008).
        if is_.is_triage():
            title = "📥 " + title
        rows.append(
            [
                display_id(is_, projects),
                colorize(status_color(is_.status()), is_.status()),
                is_.priority(),
                title,
            ]
        )
    return rows


# ---- list / batch / active ----


def filter_issues(  # noqa: PLR0913
    f_status, f_state_type, f_project, f_batch, f_milestone, f_triage=False
):
    """Load all issues and apply the given filters. Empty filters are ignored.
    status/state-type/milestone match case-insensitively exactly; batch matches
    by integer value; project matches as a substring (matching existing
    behaviour). f_triage restricts to triage items — the read-only audit lens
    over the holding pen (ADR-008 §6: `docket list --triage`); it lists ALL
    triage:true issues, including TTL-expired ones (work surfaces hide those, so
    this is the only way to see them)."""
    issues = load_all()
    out = []
    for is_ in issues:
        if f_triage and not is_.is_triage():
            continue
        if f_status != "" and is_.status().lower() != f_status.lower():
            continue
        if f_state_type != "" and is_.state_type().lower() != f_state_type.lower():
            continue
        if f_project != "" and f_project not in is_.project():
            continue
        if (
            f_batch != ""
            and str(is_.batch() if is_.batch() is not None else "")
            != str(f_batch).strip()
        ):
            continue
        if f_milestone != "" and is_.milestone().lower() != f_milestone.lower():
            continue
        out.append(is_)
    return out


def cmd_list(status, state_type, project, batch, milestone, triage=False):  # noqa: PLR0913
    out = filter_issues(status, state_type, project, batch, milestone, triage)
    if len(out) == 0:
        print("(no matching issues)")
        return
    projects, _ = load_projects()
    by_id = {is_.id(): is_ for is_ in load_all()}
    print_table(issue_rows(out, projects, by_id), ["ID", "STATUS", "PRIORITY", "TITLE"])


def _batch_work_candidate(is_):
    """Todo item that can participate in rolling-batch planning surfaces."""
    return is_.state_type() == "unstarted" and not is_.is_triage()


def todo_batches(issues):
    """Distinct batch numbers among batch-planning Todo issues, ascending."""
    return sorted(
        {
            is_.batch()
            for is_ in issues
            if _batch_work_candidate(is_) and is_.batch() is not None
        }
    )


def cmd_batch(arg):  # one cohesive batch-view command (port of batch.go)  # noqa: C901
    """Rolling-batch view: Todo split into 本批 (lowest open batch) / 下批 (next) /
    后续 (higher batches + unbatched). With a numeric arg, list just that batch."""
    issues = load_all()
    projects, _ = load_projects()
    if arg is not None and str(arg).strip() != "":
        n, ok = parse_batch(str(arg))
        if not ok:
            raise DocketError(f'invalid batch "{arg}" (want a positive integer)')
        out = [is_ for is_ in issues if _batch_work_candidate(is_) and is_.batch() == n]
        if len(out) == 0:
            print(f"(no issues in batch {n})")
            return
        sort_by_priority(out)
        print_table(issue_rows(out, projects), ["ID", "STATUS", "PRIORITY", "TITLE"])
        return
    nums = todo_batches(issues)
    cur = nums[0] if nums else None
    nxt = nums[1] if len(nums) > 1 else None
    todo = [is_ for is_ in issues if _batch_work_candidate(is_)]
    cur_items, nxt_items, rest = [], [], []
    for is_ in todo:
        b = is_.batch()
        if cur is not None and b == cur:
            cur_items.append(is_)
        elif nxt is not None and b == nxt:
            nxt_items.append(is_)
        else:
            rest.append(is_)
    for g in (cur_items, nxt_items, rest):
        sort_by_priority(g)
    sections = [
        ("本批" + (f" · batch {cur}" if cur is not None else ""), cur_items),
        ("下批" + (f" · batch {nxt}" if nxt is not None else ""), nxt_items),
        ("后续", rest),
    ]
    for label, items in sections:
        print(colorize(C_BOLD, f"{label} · {len(items)}"))
        if len(items) == 0:
            print(colorize(C_DIM, "   (无)"))
        for is_ in items:
            print(
                f"   {pad(colorize(C_CYAN, display_id(is_, projects)), 9)}  {is_.title()}"
            )
        print()


def cmd_roll(yes):
    """滚动:把下批整批批号并入本批。本批(最小未完成 batch)未清空时要二次确认。"""
    issues = load_all()
    nums = todo_batches(issues)
    if not nums:
        raise DocketError("没有带 batch 的待做 issue,无可滚动")
    cur = nums[0]
    if len(nums) <= 1:
        raise DocketError(f"只有本批 (batch {cur}),没有下批可提")
    nxt = nums[1]
    cur_open = [
        is_ for is_ in issues if _batch_work_candidate(is_) and is_.batch() == cur
    ]
    nxt_items = [
        is_ for is_ in issues if _batch_work_candidate(is_) and is_.batch() == nxt
    ]
    if cur_open and not yes:
        sys.stderr.write(
            f"本批 (batch {cur}) 还有 {len(cur_open)} 条未清空,"
            f"确认把下批 (batch {nxt}, {len(nxt_items)} 条) 并入本批?[y/N] "
        )
        sys.stderr.flush()
        resp = sys.stdin.readline().strip().lower()
        if resp not in ("y", "yes"):
            print("已取消(未滚动)", file=sys.stderr)
            raise ExitSignal(0)
    for is_ in nxt_items:
        is_.set_after("batch", str(cur), "project")
        is_.set("updated", today())
        is_.write()
        auto_commit(is_.path, f"pm(docket): {is_.id()} roll batch {nxt}→{cur}")
    print(f"rolled: batch {nxt} → {cur}({len(nxt_items)} 条提为本批)")


def wake_due(issues):
    """Open issues whose snooze has elapsed (wake ≤ today): back and worth a
    look, sorted by priority then id."""
    due = [is_ for is_ in issues if is_.is_awake_due()]
    sort_by_priority(due)
    return due


def print_wake_due_line(issues):
    """Print a one-line "N 个到期待看 (…)" hint at the top of a read view when
    any snoozed issue has woken up. No-op when N=0 (don't add noise)."""
    due = wake_due(issues)
    if not due:
        return
    projects, _ = load_projects()
    ids = ", ".join(display_id(is_, projects) for is_ in due)
    print(colorize(C_BOLD, f"⏰ {len(due)} 个到期待看") + colorize(C_DIM, f"  {ids}"))
    print()


def triage_pending(issues):
    """Active (un-TTL-expired) triage:true issues — the live review inbox/nag set
    (expired proposals self-drain). Sorted by priority then id, like wake_due."""
    pend = [is_ for is_ in issues if is_.is_triage_active()]
    sort_by_priority(pend)
    return pend


def print_triage_pending_line(issues):
    """Print a one-line "⚠ N 条待审 (docket triage)" nag at the top of a read view
    when un-accepted triage proposals are pending. No-op when N=0 (ADR-008 §5)."""
    pend = triage_pending(issues)
    if not pend:
        return
    print(colorize(C_BOLD, f"⚠ {len(pend)} 条待审 (docket triage)"))
    print()


def cmd_active(all_):
    issues = load_all()
    out = []
    for is_ in issues:
        st = is_.state_type()
        if all_ or st in ("started", "unstarted"):
            # snoozed (wake in the future) open issues are hidden from the default
            # active list — they can't be pushed now. --all still shows everything.
            if not all_ and is_.is_snoozed():
                continue
            # triage (un-accepted) proposals never enter the work face until
            # accepted — hidden from the default list, surfaced by `docket triage`.
            if not all_ and is_.is_triage():
                continue
            out.append(is_)
    sort_by_priority(out)
    print_wake_due_line(issues)
    print_triage_pending_line(issues)
    if len(out) == 0:
        print("(nothing active)")
        return
    projects, _ = load_projects()
    by_id = {is_.id(): is_ for is_ in issues}
    print_table(issue_rows(out, projects, by_id), ["ID", "STATUS", "PRIORITY", "TITLE"])


def cmd_ready():
    """The DAG frontier: Todo issues with no open blocker — start candidates.
    Soft constraints (timing windows, resource conflicts) live in comments, not
    edges, so this is a candidate set, not a guarantee."""
    issues = load_all()
    by_id = {is_.id(): is_ for is_ in issues}
    out = [
        is_
        for is_ in issues
        if is_.state_type() == "unstarted"
        and not is_.is_triage()
        and not open_blockers(is_, by_id)
    ]
    sort_by_priority(out)
    if len(out) == 0:
        print("(nothing ready)")
        return
    projects, _ = load_projects()
    print_table(issue_rows(out, projects), ["ID", "STATUS", "PRIORITY", "TITLE"])
    blocked = [
        is_
        for is_ in issues
        if is_.state_type() == "unstarted"
        and not is_.is_triage()
        and open_blockers(is_, by_id)
    ]
    if blocked:
        print(
            colorize(
                C_DIM,
                f"(候选集,软约束仍需扫一眼;另有 {len(blocked)} 条 Todo 被阻塞,"
                "见 docket active 的 ⛔)",
            )
        )


# ---- show ----


def _edge_label(bid, b):
    """Render one dependency edge: id + status + clipped title; ✓ marks a
    closed (resolved) blocker, ? a dangling ref."""
    if b is None:
        return f"{bid} ?(不存在)"
    mark = "✓ " if b.state_type() in _CLOSED_STATES else ""
    return f"{mark}{bid} {b.status()} · {clip_runes(b.title(), 40)}"


def _print_blocks_line(is_, all_issues):
    blocks = blocks_of(is_, all_issues)
    if blocks:
        labels = [_edge_label(o.id(), o) for o in blocks]
        print(f"{'blocks:':<11} {' / '.join(labels)}")


def cmd_show(  # noqa: C901, PLR0912
    id_,
    no_comments,
    *,
    all_comments=False,
    comment_idx=None,
):
    is_ = load_by_id(id_)
    projects, _ = load_projects()
    all_issues = load_all()
    by_id = {i.id(): i for i in all_issues}
    keys = [
        "id",
        "title",
        "status",
        "state_type",
        "priority",
        "project",
        "batch",
        "milestone",
        "wake",
        "parent",
        "blocked_by",
        "created",
        "updated",
        "completed",
    ]
    for k in keys:
        v, ok = is_.get(k)
        if not ok:
            if k == "blocked_by":
                _print_blocks_line(is_, all_issues)
            continue
        if k == "blocked_by":
            refs = is_.blocked_by()
            if refs:
                labels = [_edge_label(bid, by_id.get(bid)) for bid in refs]
                print(f"{'blocked_by:':<11} {' / '.join(labels)}")
            _print_blocks_line(is_, all_issues)
        elif k == "id":
            disp = display_id(is_, projects)
            if disp != is_.id():
                print(f"{'id:':<11} {disp} ({is_.id()})")
            else:
                print(f"{'id:':<11} {disp}")
        elif k == "project":
            key = unquote_scalar(v)
            p = projects.get(key)
            if p is not None and p.title != "":
                print(f"{k + ':':<11} {key} ({p.title})")
            else:
                print(f"{k + ':':<11} {key}")
        else:
            print(f"{k + ':':<11} {unquote_scalar(v)}")
    artifact = artifact_show_line(is_.id())
    if artifact is not None:
        print(f"{'artifact:':<11} {artifact}")
    print("---")
    print(is_.body, end="")
    if not is_.body.endswith("\n"):
        print()
    if no_comments:
        return
    if comment_idx is not None:
        print_comment_single(is_.id(), comment_idx)
    elif all_comments:
        print_comments_full(is_.id())
    else:
        print_comments_directory(is_.id())


def _parse_comment_blocks(id_, root=None):
    """Parse comment file into list of (header, body, byte_size) tuples."""
    if root is None:
        try:
            root = find_repo_root()
        except DocketError:
            return []
    path = str(Path(root) / "comments" / (id_ + ".md"))
    try:
        with Path(path).open(encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
    except OSError:
        return []
    if text.startswith("---\n"):
        end = text[len("---\n") :].find("\n---\n")
        if end >= 0:
            text = text[len("---\n") + end + len("\n---\n") :]
    idx = index_of_heading(text, "## ")
    if idx < 0:
        return []
    content = text[idx:]
    blocks = []
    parts = ("\n" + content).split("\n## ")[1:]
    for part in parts:
        lines = part.split("\n", 1)
        header = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        byte_size = len(("## " + part).encode("utf-8", "surrogateescape"))
        blocks.append((header, body, byte_size))
    return blocks


_KB = 1024


def _fmt_size(n):
    if n < _KB:
        return f"{n}B"
    return f"{n / _KB:.1f}KB"


def print_comments_directory(id_):
    """Show a compact directory of comments: index, timestamp, size, first content line."""
    blocks = _parse_comment_blocks(id_)
    if not blocks:
        return
    total_bytes = sum(b[2] for b in blocks)
    print(f"\n── comments ({len(blocks)} 条, {_fmt_size(total_bytes)}) ──")
    for i, (header, body, size) in enumerate(blocks, 1):
        preview = body.strip().split("\n")[0][:60] if body.strip() else ""
        print(f"  #{i:<3} {_fmt_size(size):>7}  {header}")
        if preview:
            print(f"         {preview}")
    print(f"\n  全文: docket show {id_} --all-comments")
    print(f"  单条: docket show {id_} --comment N")


def print_comment_single(id_, idx):
    """Print a single comment by 1-based index."""
    blocks = _parse_comment_blocks(id_)
    if not blocks:
        print("(no comments)")
        return
    if idx < 1 or idx > len(blocks):
        raise DocketError(f"comment #{idx} out of range (1-{len(blocks)})")
    header, body, _size = blocks[idx - 1]
    print(f"\n## {header}\n")
    print(body, end="")
    if not body.endswith("\n"):
        print()


def print_comments_full(id_):
    """Print all comments in full (legacy behavior)."""
    blocks = _parse_comment_blocks(id_)
    if not blocks:
        return
    print(f"\n## 讨论({len(blocks)} 条)\n")
    for header, body, _ in blocks:
        print(f"## {header}")
        print(body, end="")
    if blocks and not blocks[-1][1].endswith("\n"):
        print()


def print_comments(id_):
    """Backwards-compatible full comment output (used by other callers)."""
    print_comments_full(id_)


def read_comments(id_, root=None):
    """Return the comment content (everything from the first "## " block onward,
    frontmatter and "# 标题" stripped) and the number of "## " blocks. Returns
    ("", 0) if the file is missing or empty. `root` pins the lane to read from
    (its issue+comments live together); without it the single global root is
    resolved — which is wrong under the multi-tier TUI, where an aggregated
    issue's comments live in its own lane, not the cwd-resolved one."""
    if root is None:
        try:
            root = find_repo_root()
        except DocketError:
            return "", 0
    path = str(Path(root) / "comments" / (id_ + ".md"))
    try:
        with Path(path).open(encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
    except OSError:
        return "", 0
    # drop frontmatter if present
    if text.startswith("---\n"):
        end = text[len("---\n") :].find("\n---\n")
        if end >= 0:
            text = text[len("---\n") + end + len("\n---\n") :]
    # start at the first "## " (comment block), skipping "# 标题" and blanks
    idx = index_of_heading(text, "## ")
    if idx < 0:
        return "", 0
    content = text[idx:].lstrip("\n")
    n = ("\n" + content).count("\n## ")
    return content, n


def index_of_heading(text, prefix):
    """Return the byte offset of the first line that starts with prefix (at a
    line boundary), or -1."""
    if text.startswith(prefix):
        return 0
    i = text.find("\n" + prefix)
    if i >= 0:
        return i + 1
    return -1


# ---- path ----


def cmd_path(id_):
    is_ = load_by_id(id_)
    abs_ = str(Path(is_.path).resolve())
    print(abs_)


# ---- get ----


def cmd_get(id_, field):
    """Print one frontmatter field's value (or the body with `get <id> body`) — a
    light read that skips rendering the whole issue, no grep needed."""
    is_ = load_by_id(id_)
    if field == "body":
        print(is_.body, end="")
        if not is_.body.endswith("\n"):
            print()
        return
    v, ok = is_.get(field)
    if not ok:
        raise DocketError(f'{is_.id()}: no frontmatter field "{field}"')
    print(unquote_scalar(v))


# ---- new ----


def _resolve_edge_refs(is_id, refs):
    """Normalize + validate blocked_by refs: each must exist and not be self."""
    out = []
    for ref in refs:
        rid = normalize_id(ref)
        if rid == is_id:
            raise DocketError("blocked_by cannot reference the issue itself")
        try:
            load_by_id(rid)
        except DocketError:
            raise DocketError(
                f'blocked_by "{rid}" does not reference an existing issue'
            ) from None
        if rid not in out:
            out.append(rid)
    return out


# ---- by-construction body skeletons ----

#: Emitted verbatim when `new` / `projects new` get no explicit body, so writing
#: an issue = filling the blanks: the structure is baked into creation, not
#: enforced afterwards by a linter (zero-drift by construction). Each section
#: carries a one-line `<!-- 提示 -->`; a section counts as "filled" (clears the
#: `groom` placeholder metric) once it holds anything besides that hint. Task
#: issues use an SCQA-style frame; bug issues use expected/actual + MRE repro +
#: facts-vs-guess.
_BODY_TASK = """\
## 现状
<!-- 静态背景:已确立的状态/配置,不含本轮触发 -->

## 触发
<!-- 本轮为何现在冒出 + 不解决会怎样;standing/治理/方法论任务写为什么现在要处理,别硬造事故 -->

## 要做什么
<!-- 核心问题=X;done 口径=可独立验证的具体条件(优先编号清单,非"完成设计");边界=不做什么 -->

## 对人好处
<!-- 具体动作差/机制差(从 X 步降到 Y 步 / 省 N 分钟 / 从人肉判断改成规则或门禁),非"更好/更顺手" -->

<!-- 按需填(填完删本注释):
「## 成本」实施估 ≥1 session 或需多轮评审才写(实施成本 + 不解决成本)。
「## 下一步」需 principal 拍板下一步才写(ship / 等 / 立 follow-up / record only)。
-->
"""

_BODY_BUG = """\
## 预期 vs 实际
<!-- 预期该发生什么 vs 实际发生了什么(别只写"出错了") -->

## 复现步骤 (MRE)
<!-- 编号·最小·从已知初态出发·点名确切元素/字段值/等待时序——"只记一条就记这条" -->

## 环境
<!-- 版本 / OS / 分支 / commit / 相关配置 -->

## 证据
<!-- 症状(必填):日志/报错原文/截图。诊断(可选):标注"猜测",与事实分开 -->
"""

#: Project plan skeleton (projects/<key>.md body under the heading).
_BODY_PROJECT_PLAN = """\
## 目标
<!-- 这个项目要达成什么(一句话) -->

## 为什么现在成一束
<!-- 为何这些工作现在归拢成一个项目,而不是散着做 -->

## 范围·边界
<!-- 包含什么 / 明确不包含什么 -->

## done 口径
<!-- 项目算完成的可验证条件 -->
"""

#: issue --type -> body skeleton. `task` is the default (bare `new`); `bug` is the
#: reproduction-first shape. Extend here to add a type.
_NEW_TYPES = {"task": _BODY_TASK, "bug": _BODY_BUG}


def cmd_new(  # mirrors the `new` CLI flag surface 1:1 (port of new.go)  # noqa: C901, PLR0912, PLR0913, PLR0915
    title,
    project,
    priority,
    batch,
    milestone,
    parent,
    body,
    status=None,
    blocked_by=None,
    new_project=False,
    wake=None,
    directed=False,
    triage=None,
    actor=None,
    type_="task",
):
    if type_ not in _NEW_TYPES:
        raise DocketError(
            f'invalid --type "{type_}" (want one of: {"/".join(_NEW_TYPES)})'
        )
    if priority != "":
        priority = resolve_priority(priority)

    # Triage entry gate (ADR-008, fail-closed). Intent is carried explicitly, not
    # guessed from process source:
    #   --triage / --no-triage   → explicit override (highest priority)
    #   --directed               → principal 点名建,直进 Todo (not triage)
    #   else: agent context (actor != human) → triage; bare human terminal → Todo
    # Fail-closed: a missed --directed just costs one accept; the gate never lets
    # an agent flood through silently.
    if triage is True:
        is_triage_new = True
    elif triage is False or directed:
        is_triage_new = False
    else:
        gate_actor = (actor or "").strip() or default_comment_actor()
        is_triage_new = gate_actor != "human"
    # status: default Todo/unstarted; --status lands the issue elsewhere at birth
    # (e.g. backlog), so you don't create-then-restate. resolve_state validates.
    new_status, new_state_type = "Todo", "unstarted"
    if status:
        new_status, new_state_type = resolve_state(status)
    batch_val = ""
    if batch is not None and str(batch).strip() != "":
        n, ok = parse_batch(str(batch))
        if not ok:
            raise DocketError(f'invalid batch "{batch}" (want a positive integer)')
        batch_val = str(n)

    # wake — optional snooze-until date; must be a valid YYYY-MM-DD (past or future).
    wake_val = ""
    if wake is not None and str(wake).strip() != "":
        _, ok = parse_date(str(wake))
        if not ok:
            raise DocketError(f'invalid wake "{wake}" (want a YYYY-MM-DD date)')
        wake_val = str(wake).strip()

    # F3: parent — verify it exists and isn't self; field value = the id, else "~".
    parent_val = "~"
    if parent is not None and parent != "":
        pid = normalize_id(parent)
        try:
            load_by_id(pid)
        except DocketError:
            raise DocketError(
                f'parent "{pid}" does not reference an existing issue'
            ) from None
        parent_val = pid

    # project — verify it's registered (projects/<key>.md exists), mirroring the
    # parent check above. An issue tagged with an unregistered project is an
    # orphan: no display prefix, hidden from `docket projects`, surfaced only
    # later as a drift warning by `validate`. Fail fast unless --new-project asks
    # to register it now (with defaults you refine in projects/<key>.md).
    if project is not None and project != "":
        by_key, _ = load_projects()
        if project not in by_key:
            if new_project:
                cmd_project_new(project)
            else:
                raise DocketError(
                    f'project "{project}" is not registered '
                    f"(no projects/{project}.md). create it first:\n"
                    f'  docket project new {project} --title "..." --prefix XXX\n'
                    f"or pass --new-project to register it now with defaults"
                )

    issues = load_all()
    d = today()

    is_ = Issue()
    is_.fields = [
        ["domain", "pm"],
        ["id", ""],  # set in the claim loop below
        ["title", quote_scalar(title)],
        ["description", quote_scalar(title)],
        ["keywords", "[pm]"],
        ["verified", d],
        ["status", new_status],
        ["state_type", new_state_type],
        ["priority", priority],
        ["project", quote_scalar(project)],
    ]
    # batch/milestone/wake live between project and parent (logical grouping).
    if batch_val != "":
        is_.fields.append(["batch", batch_val])
    if milestone != "":
        is_.fields.append(["milestone", quote_scalar(milestone)])
    if wake_val != "":
        is_.fields.append(["wake", wake_val])
    # triage gate: holding-pen flag (state_type stays unstarted); accept揭掉字段.
    if is_triage_new:
        is_.fields.append(["triage", "true"])
    is_.fields.extend(
        [
            ["parent", parent_val],
            ["created", d],
            ["updated", d],
            ["labels", "[]"],
        ]
    )
    if blocked_by:
        # id not claimed yet — self-ref impossible, "" never matches a ref
        is_.set_blocked_by(_resolve_edge_refs("", blocked_by))
    if new_state_type == "completed":
        is_.set("completed", d)  # keep state↔completed paired (validate enforces)

    # F3: body — given non-empty (with "-" meaning stdin) overrides the default.
    if body is not None and body != "":
        content = sys.stdin.read() if body == "-" else body
        is_.body = "\n" + content.strip("\n") + "\n"
    else:
        # no --body: emit the by-construction skeleton for --type (writing =
        # filling the blanks; validate's non-empty-body check is satisfied).
        is_.body = "\n" + _NEW_TYPES[type_]

    dir_ = issues_dir()
    # Atomically claim an id: O_EXCL create fails if a concurrent `new` already
    # took this number, so we bump and retry — no lost issue from a TOCTOU race.
    n = max_id(issues) + 1
    prefix = id_prefix()
    while True:
        id_ = f"{prefix}-{n}"
        is_.set("id", id_)
        is_.path = str(Path(dir_) / (id_ + ".md"))
        try:
            fd = os.open(is_.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            n += 1
            continue  # id taken (possibly by a concurrent writer) — try next
        try:
            os.write(fd, is_.render().encode("utf-8", "surrogateescape"))
        finally:
            os.close(fd)
        break
    auto_commit(is_.path, f"pm(docket): {id_} new — {title}")
    print(f"created {is_.path}")
    if body is None or body == "":
        print("hint: body 是骨架填空; 写法详见 references/docket-writing.md")


# ---- triage entry gate: inbox / accept / decline (ADR-008) ----


def cmd_triage(gc=False):
    """List the triage inbox: every active (un-TTL-expired) `triage: true` issue
    awaiting principal review. `--gc` materializes expired proposals into canceled
    (audit-clean self-heal) before listing — they're already read-time hidden, so
    gc only tidies the on-disk record."""
    issues = load_all()
    if gc:
        expired = [is_ for is_ in issues if is_.is_triage_expired()]
        for is_ in expired:
            is_.remove("triage")  # adjudicated — leave the holding pen
            apply_state(is_, "Canceled", "canceled")  # bumps updated; clears completed
            is_.write()
            auto_commit(
                is_.path,
                f"pm(docket): {is_.id()} triage gc → Canceled (TTL {TRIAGE_TTL_DAYS}d)",
            )
        print(f"gc: {len(expired)} 条过期待审 → canceled")
        issues = load_all()  # reload post-mutation

    pend = triage_pending(issues)
    if len(pend) == 0:
        print("(triage inbox 空)")
        return
    projects, _ = load_projects()
    today_d = cn_now().date()
    rows = []
    for is_ in pend:
        cv, _ = is_.get("created")
        c, ok = parse_date(cv)
        ttl = ""
        if ok:
            ttl = str((c.date() + timedelta(days=TRIAGE_TTL_DAYS) - today_d).days)
        rows.append(
            [
                display_id(is_, projects),
                clip_runes(is_.title(), 46),
                is_.project(),
                cv.strip(),
                ttl,
            ]
        )
    print_table(rows, ["ID", "TITLE", "PROJECT", "CREATED", "TTL"])


def cmd_accept(id_, backlog=False):
    """Accept a triage proposal: clear its `triage` field → normal Todo. With
    `--backlog`, also set state_type=backlog/status=Backlog so it lands in the
    deliberate backlog instead of Todo."""
    is_ = load_by_id(id_)
    if not is_.is_triage():
        raise DocketError(f"{is_.id()} is not in triage (no triage:true field)")
    is_.remove("triage")
    if backlog:
        apply_state(is_, "Backlog", "backlog")  # sets updated too
        dest = "Backlog"
    else:
        is_.set("updated", today())
        dest = "Todo"
    is_.write()
    auto_commit(is_.path, f"pm(docket): {is_.id()} accept from triage → {dest}")
    print(f"{is_.id()} accepted → {dest}")


def cmd_decline(id_, reason=""):
    """Decline a triage proposal → canceled (audit record, not a hard delete) and
    append a `declined from triage: <reason>` comment for the trail (ADR-008)."""
    is_ = load_by_id(id_)
    if not is_.is_triage():
        raise DocketError(f"{is_.id()} is not in triage (no triage:true field)")
    is_.remove("triage")  # adjudicated — leave the holding pen
    apply_state(is_, "Canceled", "canceled")  # bumps updated; clears completed
    is_.write()
    auto_commit(is_.path, f"pm(docket): {is_.id()} decline from triage → Canceled")
    reason = (reason or "").strip()
    msg = f"declined from triage: {reason}" if reason else "declined from triage"
    # actor 承袭 default_comment_actor() (human in a bare terminal); session 不强求
    cmd_comment(is_.id(), "", msg, session="")
    print(f"{is_.id()} declined → Canceled")


# ---- project registration ----


def projects_dir() -> str:
    """The projects/ dir at the repo root, created on demand. Unlike issues_dir()
    (a read-path guard that errors when missing), this is a write path: a repo may
    legitimately have no projects/ yet when its first project is registered."""
    d = str(Path(find_repo_root()) / "projects")
    Path(d).mkdir(parents=True, exist_ok=True)
    return d


def cmd_project_new(  # noqa: PLR0913
    key,
    title="",
    prefix="",
    domain="pm",
    status="active",
    lane="non-work",
    rank="",
):
    """Register a project: write projects/<key>.md (frontmatter key/title/prefix/
    status + a heading body). The explicit counterpart to hand-writing the file —
    since `new --project X` now requires X to be registered, this is how you make
    it. prefix defaults to KEY upper-cased, title to the key; refine either by
    editing the file afterwards."""
    key = (key or "").strip()
    if key == "":
        raise DocketError("project key is required")
    if "/" in key or os.sep in key or key in (".", ".."):
        raise DocketError(f'invalid project key "{key}" (no path separators)')
    by_key, _ = load_projects()
    if key in by_key:
        raise DocketError(
            f'project "{key}" already exists ({by_key[key].path}) — '
            "edit it directly or pick another key"
        )
    title = title.strip() or key
    prefix = prefix.strip() or key.upper()
    domain = domain.strip() or "pm"
    status = status.strip() or "active"
    lane = lane.strip() or "non-work"
    rank = str(rank or "").strip()
    d = today()
    frontmatter = [
        "domain: " + domain,
        "lane: " + lane,
    ]
    if rank:
        frontmatter.append("rank: " + rank)
    frontmatter.extend(
        [
            "key: " + key,
            "title: " + quote_scalar(title),
            "prefix: " + prefix,
            "status: " + status,
            "created: " + d,
            "updated: " + d,
        ]
    )
    content = (
        "---\n"
        + "\n".join(frontmatter)
        + "\n---\n\n"
        + f"# {title} ({prefix})\n\n"
        + _BODY_PROJECT_PLAN
    )
    path = str(Path(projects_dir()) / (key + ".md"))
    # O_EXCL: never clobber an existing file (case-collision / race safety),
    # mirroring the id-claim loop in cmd_new.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise DocketError(f"project file already exists: {path}") from None
    try:
        os.write(fd, content.encode("utf-8", "surrogateescape"))
    finally:
        os.close(fd)
    auto_commit(path, f"pm(docket): project {key} new — {title}")
    print(f"created {path}")


# ---- start / finish / status ----


def apply_state(is_, status, state_type):
    """Set status/state_type + updated, and keep the completed date consistent
    with the state: stamped on entering completed, cleared on leaving (validate
    enforces this pairing). Any explicit workflow state change also adjudicates
    a triage proposal: the item has left the holding pen, so stale `triage:true`
    must not keep it hidden or nagging after start/finish/status/set."""
    is_.remove("triage")
    is_.set("status", status)
    is_.set("state_type", state_type)
    is_.set("updated", today())
    if state_type == "completed":
        is_.set("completed", today())
    else:
        is_.remove("completed")


def set_state_cmd(id_, name, status, state_type):
    """Apply a fixed (status, state_type) to an issue + updated/completed."""
    is_ = load_by_id(id_)
    ensure_worktrees_reconciled(is_, state_type)
    apply_state(is_, status, state_type)
    is_.write()
    auto_commit(is_.path, f"pm(docket): {is_.id()} {name} → {status}")
    print(f"{is_.id()} -> {status} ({state_type})")
    if name == "finish":
        print_asset_reminder()


def print_asset_reminder():
    """Soft 资产自检 prompt on the finish success path: nudge the agent to land
    durable assets (ADR/spec/contract/architecture/runbook) before closing an
    issue — agents routinely forget. Not a gate: many issues have no asset, so it
    never blocks/asks, just reminds. Skipped under DOCKET_NO_ASSET_REMINDER=1
    (scripts/batch). Goes to stderr so the machine-readable "-> Done" stdout line
    stays clean for greppers; colored only on a real TTY (render conventions)."""
    if os.environ.get("DOCKET_NO_ASSET_REMINDER") == "1":
        return
    lines = [
        colorize(C_DIM, "── 资产自检(有则落,没有跳过)──"),
        f"  {colorize(C_BOLD, '拍板了方案/方向?')}      → ADR(decisions/)",
        f"  {colorize(C_BOLD, '定了约束/口径?')}        → spec/contract(contracts/ 或仓内 docs/)",
        f"  {colorize(C_BOLD, '系统架构变了?')}        → 更新 <repo>:docs:architecture",
        f"  {colorize(C_BOLD, '踩了新坑?')}            → runbook",
    ]
    print("\n".join(lines), file=sys.stderr)


def cmd_start(id_):
    set_state_cmd(id_, "start", "In Progress", "started")


def cmd_finish(id_):
    set_state_cmd(id_, "finish", "Done", "completed")


def cmd_status(id_, state):
    is_ = load_by_id(id_)
    status, state_type = resolve_state(state)
    ensure_worktrees_reconciled(is_, state_type)
    apply_state(is_, status, state_type)
    is_.write()
    auto_commit(is_.path, f"pm(docket): {is_.id()} status → {status} ({state_type})")
    print(f"{is_.id()} -> {status} ({state_type})")


# ---- set ----


def cmd_set(  # mirrors the `set` CLI flag surface 1:1 (port of set.go)  # noqa: C901, PLR0912, PLR0913, PLR0915
    id_,
    priority=None,
    project=None,
    batch=None,
    milestone=None,
    title=None,
    parent=None,
    status=None,
    blocked_by=None,
    unblock=None,
    wake=None,
    unwake=False,
):
    """Edit one or more frontmatter fields on an issue, validate enum/format, and
    bump updated=today. batch/milestone/wake are inserted after project (before
    parent) when absent, matching new's layout. --status changes the workflow
    state (and its paired completed date) in the same call, so you don't
    context-switch to the `status` verb. --wake snoozes the issue until a date
    (hidden from active/overview while future); --unwake clears it."""
    is_ = load_by_id(id_)
    changed = []

    if status is not None:
        st_status, st_state_type = resolve_state(status)
        ensure_worktrees_reconciled(is_, st_state_type)
        apply_state(is_, st_status, st_state_type)  # sets updated + completed too
        changed.append("status")

    if priority is not None:
        priority = resolve_priority(priority)
        is_.set("priority", priority)
        changed.append("priority")

    if project is not None:
        is_.set("project", quote_scalar(project))
        changed.append("project")

    if batch is not None:
        n, ok = parse_batch(str(batch))
        if not ok:
            raise DocketError(f'invalid batch "{batch}" (want a positive integer)')
        is_.set_after("batch", str(n), "project")
        changed.append("batch")

    if milestone is not None:
        if milestone.strip() == "":
            raise DocketError("milestone must be non-empty")
        anchor = "project"
        _, has_batch = is_.get("batch")
        if has_batch:
            anchor = "batch"  # keep project, batch, milestone, parent order
        is_.set_after("milestone", quote_scalar(milestone), anchor)
        changed.append("milestone")

    if unwake and is_.remove("wake"):
        changed.append("wake")

    if wake is not None:
        _, ok = parse_date(str(wake))
        if not ok:
            raise DocketError(f'invalid wake "{wake}" (want a YYYY-MM-DD date)')
        is_.set_wake(str(wake).strip())
        changed.append("wake")

    if title is not None:
        if title.strip() == "":
            raise DocketError("title must be non-empty")
        is_.set("title", quote_scalar(title))
        changed.append("title")

    if parent is not None:
        pid = normalize_id(parent)
        if pid == is_.id():
            raise DocketError(f'parent "{pid}" cannot reference the issue itself')
        try:
            load_by_id(pid)
        except DocketError:
            raise DocketError(
                f'parent "{pid}" does not reference an existing issue'
            ) from None
        is_.set("parent", pid)
        changed.append("parent")

    if blocked_by or unblock:
        cur = is_.blocked_by()
        for rid in _resolve_edge_refs(is_.id(), blocked_by or []):
            if rid not in cur:
                cur.append(rid)
        for ref in unblock or []:
            rid = normalize_id(ref)
            if rid not in cur:
                raise DocketError(f"{is_.id()} is not blocked by {rid}")
            cur.remove(rid)
        is_.set_blocked_by(cur)
        changed.append("blocked_by")

    if len(changed) == 0:
        raise DocketError(
            "set: no fields given "
            "(try --status/--priority/--project/--batch/--milestone/--title/--parent/--blocked-by/--unblock)"
        )
    is_.set("updated", today())
    is_.write()
    auto_commit(is_.path, f"pm(docket): {is_.id()} set {', '.join(changed)}")
    print(f"updated {Path(is_.path).name}")


# ---- search ----


class _SearchHit:
    __slots__ = ("is_", "snippets")

    def __init__(self, is_):
        self.is_ = is_
        self.snippets = []


def cmd_search(  # noqa: C901, PLR0912
    kw,
):  # one grep-across-issues+comments command (port of search.go)
    """grep issues/*.md (title + body) and comments/*.md (comment bodies) for kw,
    case-insensitively. Structured frontmatter is NOT searched. One line per
    matched issue, sorted by id, deduped, with indented source-tagged snippets."""
    kw = kw.strip()
    if kw == "":
        raise DocketError("usage: docket search <kw>")
    lkw = kw.lower()

    issues = load_all()
    by_id = {}
    hits = {}

    def add_snippet(is_, snip):
        h = hits.get(is_.id())
        if h is None:
            h = _SearchHit(is_)
            hits[is_.id()] = h
        h.snippets.append(snip)

    for is_ in issues:
        by_id[is_.id()] = is_
        # title (the only frontmatter field searched; rest are list filters)
        if lkw in is_.title().lower():
            add_snippet(is_, "[title] " + snippet(is_.title(), kw))
        # body — report each matching line
        for line in is_.body.split("\n"):
            if lkw in line.lower():
                add_snippet(is_, "[body] " + snippet(line.strip(), kw))

    # comments: comments/<id>.md -> <id> (list all; the by_id map filters to
    # known issues, so mixed/non-default canonical prefixes are not dropped)
    root = find_repo_root()
    cpaths = [str(p) for p in (Path(root) / "comments").glob("*.md")]
    cpaths.sort()
    for p in cpaths:
        base = Path(p).name
        if base.endswith(".md"):
            base = base[: -len(".md")]
        is_ = by_id.get(base)
        if is_ is None:
            continue  # orphan comment file; skip (no issue to attribute to)
        content, n = read_comments(base)
        if n == 0:
            continue
        for line in content.split("\n"):
            ll = line.strip()
            if ll in ("", "---") or ll.startswith("## "):
                continue  # skip separators and "## author · time" headers
            if lkw in line.lower():
                add_snippet(is_, "[comment] " + snippet(ll, kw))

    if len(hits) == 0:
        print(f'no match for "{kw}"')
        return

    ids = list(hits.keys())
    ids.sort(key=lambda x: id_num(x)[0])
    for id_ in ids:
        h = hits[id_]
        print(f"{h.is_.id()}  {h.is_.status()}  {h.is_.title()}")
        for s in h.snippets:
            print(f"    {s}")


def snippet(s, kw):
    """Return a short context window around the first (case-insensitive)
    occurrence of kw in s, with the match left intact. Whole string if short."""
    s = s.strip()
    ctx = 30
    idx = s.lower().find(kw.lower())
    if idx < 0:
        # shouldn't happen (caller already matched) — return a clipped head
        return clip_runes(s, 2 * ctx + len(kw))
    # Python str is already codepoint-indexed; idx/len are rune positions.
    start_rune = idx
    end_rune = start_rune + len(kw)
    lo = start_rune - ctx
    lo = max(lo, 0)
    hi = end_rune + ctx
    hi = min(hi, len(s))
    out = s[lo:hi]
    if lo > 0:
        out = "…" + out
    if hi < len(s):
        out = out + "…"
    return out


# ---- comment ----


def _last_comment_start(text):
    """Byte offset of the last comment block (a line starting with '## '), or -1."""
    i = text.rfind("\n## ")
    if i >= 0:
        return i + 1
    return 0 if text.startswith("## ") else -1


def _first_env(keys):
    for key in keys:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def default_comment_actor():
    # ADR-008: explicit DOCKET_* wins; then process-source probes. The Claude
    # branch's real markers are CLAUDE_CODE_SESSION_ID / CLAUDECODE=1 / AI_AGENT
    # (the old CLAUDE_SESSION_ID/CLAUDECODE_SESSION_ID names never existed, so
    # adr-005 silently mis-signed Claude comments as human); the legacy names are
    # kept in the or-chain for backward compatibility.
    actor = _first_env(("DOCKET_COMMENT_ACTOR", "DOCKET_ACTOR"))
    if actor:
        return actor
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI"):
        return "codex"
    if _first_env(
        (
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDECODE",
            "AI_AGENT",
            "CLAUDE_SESSION_ID",
            "CLAUDECODE_SESSION_ID",
        )
    ):
        return "claude"
    return "human"


def default_comment_session():
    return _first_env(
        (
            "DOCKET_COMMENT_SESSION",
            "CODEX_THREAD_ID",
            "CLAUDE_SESSION_ID",
            "CLAUDECODE_SESSION_ID",
        )
    )


def _comment_block(actor, text, session=None):
    ts = cn_now().strftime("%Y-%m-%dT%H:%M")
    actor = (actor or "").strip() or default_comment_actor()
    session = default_comment_session() if session is None else session.strip()
    suffix = f" · session {session}" if session else ""
    return f"## {ts} · {actor}{suffix}\n\n{text}\n\n---\n\n"


def cmd_comment(  # noqa: PLR0913
    id_, actor, text, amend=False, delete_last=False, session=None
):  # append/amend/delete-last share one entry (port of comment.go)
    """Append a comment block to <root>/comments/ISSUE-<n>.md (creating it with
    frontmatter if absent). --amend replaces the last block with new text;
    --delete-last drops the last block. Block: "## <time> · <actor>" + body."""
    id_ = normalize_id(id_)
    load_by_id(id_)  # issue must exist (so we don't create orphan comment files)
    root = find_repo_root()
    dir_ = str(Path(root) / "comments")
    path = str(Path(dir_) / (id_ + ".md"))
    actor = (actor or "").strip() or default_comment_actor()

    if amend or delete_last:
        op = "amend" if amend else "delete-last"
        if not Path(path).exists():
            raise DocketError(f"no comments on {id_} to {op}")
        with Path(path).open(encoding="utf-8", errors="surrogateescape") as f:
            existing = f.read()
        start = _last_comment_start(existing)
        if start < 0:
            raise DocketError(f"no comment block on {id_} to {op}")
        base = existing[:start].rstrip("\n") + "\n\n"  # head + all earlier blocks
        if amend:
            text = text.strip()
            if text == "":
                raise DocketError("comment text must be non-empty")
            base += _comment_block(actor, text, session)
        with Path(path).open("w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(base)
        auto_commit(path, f"pm(docket): {id_} comment {op} ({actor})")
        print(f"{op} on {Path(path).name}")
        return

    text = text.strip()
    if text == "":
        raise DocketError("comment text must be non-empty")
    Path(dir_).mkdir(mode=0o755, parents=True, exist_ok=True)
    block = _comment_block(actor, text, session)
    if not Path(path).exists():
        header = (
            f"---\ndomain: pm\nid: {id_}\ntype: comments\nsource: local\n"
            f"imported: {today()}\nkeywords: [pm, comments]\n---\n\n# {id_} 讨论\n\n"
        )
        with Path(path).open("w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(header + block)
    else:
        with Path(path).open("a", encoding="utf-8", errors="surrogateescape") as f:
            f.write(block)
    auto_commit(path, f"pm(docket): {id_} comment ({actor})")
    print(f"appended comment to {Path(path).name}")


# ---- roundtrip ----


def cmd_roundtrip(id_):
    """Parse an issue and write it back verbatim (no field changes). Fidelity
    check: `git diff` on the file must stay empty."""
    is_ = load_by_id(id_)
    is_.write()
    print(f"rewrote {Path(is_.path).name} verbatim")


# ---- validate ----


def parse_date(s):
    """Parse a YYYY-MM-DD frontmatter date. ok=False on a non-empty value that
    doesn't parse (so callers can report a format problem)."""
    s = s.strip()
    if s == "":
        return None, False
    try:
        return datetime.strptime(s, "%Y-%m-%d"), True
    except ValueError:
        return None, False


# One pass over all data-integrity checks (port of validate.go).
def collect_validation_problems(issues, strict=False):  # noqa: C901, PLR0912, PLR0915
    """Run every data-integrity check over `issues` and return the sorted list of
    problem strings (empty when clean). Pure: no printing, no exit — `cmd_validate`
    handles output/ExitSignal, and `cmd_groom` reuses this for its validate summary.
    `strict=True` adds work-surface health checks intended for closeout gates."""
    ids = {}
    for is_ in issues:
        ids[is_.id()] = True
    projects, _ = load_projects()
    problems = []
    for is_ in issues:
        name = Path(is_.path).name
        # required fields (id/title/status/state_type) — present and non-empty
        for req in ["id", "title", "status", "state_type"]:
            v, ok = is_.get(req)
            if not ok or unquote_scalar(v).strip() == "":
                problems.append(f'{name}: missing/empty required field "{req}"')
        # title / description non-empty (when the field exists)
        for req in ["title", "description"]:
            v, ok = is_.get(req)
            if ok and unquote_scalar(v).strip() == "":
                problems.append(f"{name}: {req} is empty")
        # filename matches id
        expected = is_.id() + ".md"
        if is_.id() != "" and expected != name:
            problems.append(f'{name}: filename does not match id "{is_.id()}"')
        # state_type valid
        st = is_.state_type()
        if st != "" and not valid_state_type(st):
            problems.append(
                f'{name}: invalid state_type "{st}" (want backlog/unstarted/started/completed/canceled)'
            )
        # status <-> state_type consistency
        if st != "" and valid_state_type(st):
            want = STATE_TYPE_TO_STATUS[st]
            if is_.status() != want:
                problems.append(
                    f'{name}: status "{is_.status()}" inconsistent with state_type "{st}" (want status "{want}")'
                )
        # priority value in enum (when present)
        p = is_.priority()
        if p != "" and not valid_priority(p):
            problems.append(
                f'{name}: invalid priority "{p}" (want Urgent/High/Medium/Low/"No priority")'
            )
        # parent: points to an existing id (when not ~) and is not self
        par = is_.parent()
        if par not in ("", "~"):
            if par not in ids:
                problems.append(
                    f'{name}: parent "{par}" does not reference an existing issue'
                )
            if par == is_.id():
                problems.append(f"{name}: parent references itself")
        # project: a non-empty key must name a registered projects/<key>.md. Empty
        # project remains valid in default mode for legacy/current loose data, but
        # strict mode treats in-scope issue without a project as work-surface drift.
        proj = is_.project()
        if proj != "" and proj not in projects:
            problems.append(
                f'{name}: project "{proj}" does not reference an existing project'
            )
        if strict and proj == "" and not is_.is_triage() and st != "canceled":
            problems.append(
                f"{name}: project is empty (strict mode requires project for in-scope issues)"
            )
        # blocked_by: refs exist, no self-ref, no duplicates
        bb = is_.blocked_by()
        for bid in bb:
            if bid not in ids:
                problems.append(
                    f'{name}: blocked_by "{bid}" does not reference an existing issue'
                )
            if bid == is_.id():
                problems.append(f"{name}: blocked_by references itself")
        if len(bb) != len(set(bb)):
            problems.append(f"{name}: blocked_by has duplicate entries")
        # batch valid (positive integer when present)
        b, has_batch = is_.get("batch")
        if has_batch:
            _, ok = parse_batch(b)
            if not ok:
                problems.append(
                    f'{name}: invalid batch "{b.strip()}" (want a positive integer)'
                )
        # milestone non-empty when the field exists
        v, ok = is_.get("milestone")
        if ok and unquote_scalar(v).strip() == "":
            problems.append(f"{name}: milestone field present but empty")
        # wake: valid YYYY-MM-DD (past or future) when the field exists
        wv, has_wake = is_.get("wake")
        if has_wake:
            _, wok = parse_date(wv)
            if not wok:
                problems.append(
                    f'{name}: invalid wake "{wv.strip()}" (want a YYYY-MM-DD date)'
                )
        # triage: must be a boolean ("true"/"false") when the field exists (ADR-008)
        tv, has_triage = is_.get("triage")
        if has_triage:
            _, tok = parse_bool(tv)
            if not tok:
                problems.append(
                    f'{name}: invalid triage "{tv.strip()}" (want true/false)'
                )
        # body non-empty
        if is_.body.strip() == "":
            problems.append(f"{name}: body is empty")
        # date logic
        _, has_completed_field = is_.get("completed")
        completed_state = st == "completed"
        if completed_state and not has_completed_field:
            problems.append(f"{name}: state_type completed but no completed date")
        if has_completed_field and not completed_state:
            problems.append(
                f'{name}: has completed date but state_type is "{st}" (not completed)'
            )
        created_v, _ = is_.get("created")
        updated_v, _ = is_.get("updated")
        created, created_ok = parse_date(created_v)
        updated, updated_ok = parse_date(updated_v)
        _, has_created = is_.get("created")
        if has_created and not created_ok:
            problems.append(f"{name}: created is not a valid YYYY-MM-DD date")
        _, has_updated = is_.get("updated")
        if has_updated and not updated_ok:
            problems.append(f"{name}: updated is not a valid YYYY-MM-DD date")
        if created_ok and updated_ok and updated < created:
            problems.append(
                f"{name}: updated ({updated.strftime('%Y-%m-%d')}) is before created ({created.strftime('%Y-%m-%d')})"
            )
        if has_completed_field:
            completed_v, _ = is_.get("completed")
            completed, completed_ok = parse_date(completed_v)
            if not completed_ok:
                problems.append(f"{name}: completed is not a valid YYYY-MM-DD date")
            if completed_ok and created_ok and completed < created:
                problems.append(
                    f"{name}: completed ({completed.strftime('%Y-%m-%d')}) is before created ({created.strftime('%Y-%m-%d')})"
                )
    # blocked_by cycles (iterative three-color DFS over the dependency graph)
    graph = {is_.id(): [b for b in is_.blocked_by() if b in ids] for is_ in issues}
    color = dict.fromkeys(graph, 0)  # 0 white / 1 on-stack / 2 done
    for root in graph:
        if color[root] != 0:
            continue
        stack = [(root, iter(graph[root]))]
        color[root] = 1
        path = [root]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = 2
                stack.pop()
                path.pop()
                continue
            if color[nxt] == 1:
                cyc = [*path[path.index(nxt) :], nxt]
                problems.append(f"blocked_by cycle: {' -> '.join(cyc)}")
            elif color[nxt] == 0:
                color[nxt] = 1
                stack.append((nxt, iter(graph[nxt])))
                path.append(nxt)
    problems.sort()
    return problems


def cmd_validate(strict=False):
    issues = load_all()
    problems = collect_validation_problems(issues, strict=strict)
    if len(problems) > 0:
        for p in problems:
            print(p, file=sys.stderr)
        print(
            f"\nvalidate: {len(problems)} problem(s) across {len(issues)} issue(s)",
            file=sys.stderr,
        )
        raise ExitSignal(1)
    suffix = " (strict)" if strict else ""
    print(f"validate: OK{suffix} — {len(issues)} issue(s) clean")


# ---- groom (periodic staleness triage) ----

#: Status-group ordering for the staleness table: In Progress first, then Todo,
#: then Backlog. Non-done issues only ever carry these three statuses (state_type
#: started/unstarted/backlog); anything else sorts last (9) defensively.
_GROOM_STATUS_ORDER = {"In Progress": 0, "Todo": 1, "Backlog": 2}

#: Title clip width for the staleness table (code points, "…" appended if longer).
_GROOM_TITLE_CLIP = 46


def _groom_age_days(updated_v, today_d):
    """Stalled days = today − the issue's `updated` date, or -1 when `updated` is
    missing/unparseable (so a bad date is loud, not silently 0)."""
    parsed, ok = parse_date(updated_v)
    if not ok:
        return -1
    return (today_d - parsed.date()).days


def _groom_rows(issues, projects, today_d):
    """Build one staleness record per non-done issue, sorted by status group
    (In Progress→Todo→Backlog) then stalled-days descending. Each record carries
    the logical values; rendering (table vs json) is the caller's job."""
    records = []
    for is_ in issues:
        if is_.state_type() in _CLOSED_STATES:
            continue  # done/canceled are out of scope
        if is_.is_triage():
            continue  # un-accepted triage proposals aren't groomed (ADR-008)
        _, n_comments = read_comments(is_.id())
        par = is_.parent()
        records.append(
            {
                "id": display_id(is_, projects),
                "status": is_.status(),
                "age": _groom_age_days(is_.get("updated")[0], today_d),
                "priority": is_.priority(),
                "project": is_.project(),
                "parent": par if par not in ("", "~") else "-",
                "comments": n_comments,
                "title": is_.title(),
            }
        )
    records.sort(key=lambda r: (_GROOM_STATUS_ORDER.get(r["status"], 9), -r["age"]))
    return records


# ---- writing-health metrics for the by-construction skeletons.
# Read-time, zero side effects. ----

#: Comment-length advisory thresholds for the KB's "把长篇分析灌进 comment"
#: slop signal (industrial-issue-writing-standards F5). Warnings stay advisory:
#: long artifacts belong in KB/docs/artifacts, with comment holding a link and
#: one-line conclusion.
_COMMENT_WARN_CHARS = 1500
_COMMENT_ALARM_CHARS = 2500
_COMMENT_LONG_CHARS = _COMMENT_WARN_CHARS  # compatibility alias for older callers


def _unfilled_sections(body: str) -> int:
    """Count `## ` sections still in their skeleton state: heading present but the
    only content under it is the inline `<!-- 提示 -->` hint. Content-based (no
    sentinel): writing anything real under a heading clears it even if the hint
    stays. A skeletonless body scores 0."""
    no_comments = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    # re.split drops the delimiters -> [preamble, sec1, sec2, ...]; a section is
    # "unfilled" when the text between its heading and the next is blank.
    sections = re.split(r"(?m)^##[^\n]*$", no_comments)[1:]
    return sum(1 for s in sections if s.strip() == "")


def _comment_block_lengths(id_: str, root: str | None = None) -> list[int]:
    """Character length of each comment block body in comments/<id>.md (the
    `## <time> · actor` header + `---` separator stripped). Empty list if none."""
    content, n = read_comments(id_, root)
    if n == 0:
        return []
    out = []
    for block in re.split(r"(?m)^## .*$", content):
        text = re.sub(r"\n-{3,}\s*$", "", block.strip()).strip()
        if text:
            out.append(len(text))
    return out


def _pct(values, p):
    """Nearest-rank percentile of an unsorted list (0 for empty)."""
    if not values:
        return 0
    s = sorted(values)
    k = (p * len(s) + 99) // 100 - 1  # integer ceil(p*n/100) - 1
    return s[max(0, min(len(s) - 1, k))]


_LONG_WORK_BODY_MARKERS = (
    "当前状态卡",
    "Current Status Card",
    "阶段出口",
    "Stage Exit Contract",
    "Split Ledger",
    "北极星",
    "north-star",
    "长期",
)

_LONG_WORK_STATUS_CARD_FIELDS = (
    (
        "当前阶段",
        "missing_status_card_field",
        "缺当前阶段",
        "当前状态卡缺少当前阶段字段。",
        "补当前阶段，说明这条长线现在处在哪个 stage。",
    ),
    (
        "下一步最小动作",
        "missing_next_action",
        "缺下一步最小动作",
        "当前状态卡缺少下一步最小动作。",
        "补下一步最小动作，约束 agent 下一轮只推进一个可闭环动作。",
    ),
    (
        "进入实现闸门",
        "unclear_implementation_gate",
        "实现闸门不清",
        "当前状态卡缺少进入实现闸门。",
        "补进入实现闸门，说明什么证据满足后才能进入实现或下一阶段。",
    ),
    (
        "不做什么",
        "missing_status_card_field",
        "缺不做范围",
        "当前状态卡缺少不做范围。",
        "补不做范围，避免 agent 把长线扩成无边界的前置项。",
    ),
)


def _has_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


def _is_long_work_candidate(is_, child_counts: dict[str, int]) -> bool:
    """Low-noise long-running-work detector for soft groom hints.

    A candidate is either already using the long-work vocabulary in its body/title,
    or is acting as a real umbrella (2+ open children). Single-child parents are
    intentionally ignored to avoid nagging normal leaf decomposition.
    """
    text = f"{is_.title()}\n{is_.body}"
    return child_counts.get(is_.id(), 0) >= 2 or _has_any_marker(  # noqa: PLR2004
        text, _LONG_WORK_BODY_MARKERS
    )


def _section_body(body: str, heading: str) -> str:
    m = re.search(rf"(?m)^##\s+{re.escape(heading)}\s*$", body)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"(?m)^##\s+", body[start:])
    end = start + nxt.start() if nxt else len(body)
    return body[start:end].strip()


def _long_work_signals(issues, projects) -> tuple[int, list[dict]]:  # noqa: C901, PLR0912
    """Structured advisory signals for long-running agent work.

    These are deliberately NOT validation errors. They only surface low-noise
    structural gaps that make long work hard to hand off: missing current card,
    missing stage exit, missing next action / implementation gate, and split
    ledgers that do not record whether the original exit changed.
    """
    child_counts: dict[str, int] = {}
    for is_ in issues:
        if is_.state_type() in _CLOSED_STATES or is_.is_triage():
            continue
        par = is_.parent()
        if par not in ("", "~"):
            child_counts[par] = child_counts.get(par, 0) + 1

    candidate_count = 0
    signals: list[dict] = []
    for is_ in issues:
        if is_.state_type() in _CLOSED_STATES or is_.is_triage():
            continue
        if not _is_long_work_candidate(is_, child_counts):
            continue

        candidate_count += 1
        body = is_.body
        display = display_id(is_, projects)
        seen: set[tuple[str, str]] = set()

        def add_signal(  # noqa: PLR0913
            kind: str,
            label: str,
            reason: str,
            recommended_action: str,
            field: str = "",
            confidence: str = "high",
        ) -> None:
            key = (kind, field)
            if key in seen:  # noqa: B023
                return
            seen.add(key)  # noqa: B023
            signals.append(
                {
                    "issue_id": is_.id(),  # noqa: B023
                    "display_id": display,  # noqa: B023
                    "title": is_.title(),  # noqa: B023
                    "kind": kind,
                    "label": label,
                    "severity": "advisory",
                    "confidence": confidence,
                    "reason": reason,
                    "recommended_action": recommended_action,
                    "field": field,
                }
            )

        card = _section_body(body, "当前状态卡")
        if not card:
            add_signal(
                "missing_status_card",
                "缺当前状态卡",
                "长期候选 issue 没有 `## 当前状态卡`。",
                "补当前状态卡，压缩北极星、当前阶段、最近完成、下一步、实现闸门和不做范围。",
                field="当前状态卡",
            )
        else:
            for (
                field,
                kind,
                label,
                reason,
                recommended_action,
            ) in _LONG_WORK_STATUS_CARD_FIELDS:
                if field not in card:
                    add_signal(
                        kind,
                        label,
                        reason,
                        recommended_action,
                        field=field,
                    )

        if "阶段出口" not in body and "Stage Exit Contract" not in body:
            add_signal(
                "missing_stage_exit",
                "缺阶段出口",
                "长期候选 issue 没有阶段出口或 Stage Exit Contract。",
                "补阶段出口，说明本阶段要证明什么、不能用什么冒充退出，以及何时 review。",
                field="阶段出口",
            )

        if "下一步最小动作" not in body:
            add_signal(
                "missing_next_action",
                "缺下一步最小动作",
                "长期候选 issue 没有下一步最小动作。",
                "补下一步最小动作，避免下一轮 agent 继续自由扩展范围。",
                field="下一步最小动作",
            )

        if "进入实现闸门" not in body and "实现闸门" not in body:
            add_signal(
                "unclear_implementation_gate",
                "实现闸门不清",
                "长期候选 issue 没有清楚说明何时允许进入实现或下一阶段。",
                "补实现闸门，写明哪些证据满足后才能开始实现、迁移或切默认路径。",
                field="进入实现闸门",
            )

        split = _section_body(body, "Split Ledger") or _section_body(body, "拆分记录")
        if split and "原 exit" not in split and "出口变化" not in split:
            add_signal(
                "split_without_exit_delta",
                "split 缺出口变化",
                "Split Ledger 没有说明原 exit 是否变化。",
                "补原 exit 或出口变化说明，避免拆分记录把漂移合法化。",
                field="Split Ledger",
            )

    return candidate_count, signals


def _work_health(issues, projects, project: str = "") -> dict:
    """Agent-facing structured health signal.

    This is the primary machine-readable surface. Human-facing commands such as
    `groom` may render these signals, but should not be the only API an agent can
    consume when producing a progress/drift report.
    """
    long_work_candidates, signals = _long_work_signals(issues, projects)
    return {
        "schema_version": 1,
        "source": "docket.work_health",
        "scope": {"project": project},
        "summary": {
            "long_work_candidates": long_work_candidates,
            "signal_count": len(signals),
        },
        "signals": signals,
    }


def _long_work_health_from_signals(health: dict) -> dict:
    grouped: dict[str, list[str]] = {}
    for signal in health["signals"]:
        display = signal["display_id"]
        grouped.setdefault(display, [])
        label = signal["label"]
        if label not in grouped[display]:
            grouped[display].append(label)
    return {
        "candidates": health["summary"]["long_work_candidates"],
        "hints": [
            {"id": display, "hints": labels} for display, labels in grouped.items()
        ],
    }


def _long_work_health(issues, projects) -> dict:
    """Compatibility wrapper for the old groom footer hint shape."""
    return _long_work_health_from_signals(_work_health(issues, projects))


def cmd_health(project="", as_json=False):
    """Agent-facing PM work-health signal.

    The JSON form is the intended API for agents that need to answer progress,
    drift, conflict, or mission-control questions. It is advisory only: no
    validate failure, no schema migration, and no alternate read path.
    """
    all_issues = load_all()
    issues = (
        [is_ for is_ in all_issues if project in is_.project()]
        if project != ""
        else all_issues
    )
    projects, _ = load_projects()
    health = _work_health(issues, projects, project=project)

    if as_json:
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return

    summary = health["summary"]
    print(
        f"工作健康: 长期候选 {summary['long_work_candidates']} 条 · "
        f"信号 {summary['signal_count']} 条"
    )
    for signal in health["signals"][:20]:
        print(
            f"- {signal['display_id']}: {signal['label']} "
            f"({signal['kind']}) — {signal['recommended_action']}"
        )
    if len(health["signals"]) > 20:  # noqa: PLR2004
        print(f"... 另 {len(health['signals']) - 20} 条")


def _writing_health(issues, root):
    """Aggregate the by-construction writing-health signals over `issues`: how many
    non-done issues still hold unfilled skeleton sections, plus the comment-length
    distribution + warn/alarm counts (long-analysis-as-comment is the KB's main
    slop mode)."""
    unfilled_issues = 0
    unfilled_sections = 0
    lengths = []
    for is_ in issues:
        if is_.state_type() not in _CLOSED_STATES and not is_.is_triage():
            u = _unfilled_sections(is_.body)
            if u:
                unfilled_issues += 1
                unfilled_sections += u
        lengths.extend(_comment_block_lengths(is_.id(), root))
    return {
        "unfilled_issues": unfilled_issues,
        "unfilled_sections": unfilled_sections,
        "comment_count": len(lengths),
        "comment_p50": _pct(lengths, 50),
        "comment_p95": _pct(lengths, 95),
        "comment_max": max(lengths) if lengths else 0,
        "comment_warn": sum(1 for x in lengths if x > _COMMENT_WARN_CHARS),
        "comment_alarm": sum(1 for x in lengths if x > _COMMENT_ALARM_CHARS),
    }


def cmd_groom(project="", as_json=False, today_str=""):  # noqa: C901, PLR0912
    """Periodic mechanical inventory of every non-done issue (state_type ∉
    {completed, canceled}) — the deterministic step the docket-groom skill drives
    before any LLM fan-out. One row per issue: status / stalled-days (today −
    `updated`) / display id / priority / project / parent / 讨论条数 / title,
    grouped by status (In Progress→Todo→Backlog) then stalled-days descending.

    today defaults to the current date (UTC+8, matching the rest of docket);
    --today YYYY-MM-DD pins it (for tests / reproducible reports). --project filters
    to one project (substring match, like `list`). --json emits the records for
    agent fan-out. The table footer reuses `validate` for a data-health summary
    plus the non-done total. Zero network / zero LLM."""
    if today_str.strip() != "":
        parsed, ok = parse_date(today_str)
        if not ok:
            raise DocketError(f'invalid --today "{today_str}" (want a YYYY-MM-DD date)')
        today_d = parsed.date()
    else:
        today_d = cn_now().date()

    all_issues = load_all()
    issues = (
        [is_ for is_ in all_issues if project in is_.project()]
        if project != ""
        else all_issues
    )
    projects, _ = load_projects()
    records = _groom_rows(issues, projects, today_d)

    if as_json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return

    headers = [
        "STATUS",
        "AGE",
        "ID",
        "PRIORITY",
        "PROJECT",
        "PARENT",
        "#",
        "TITLE",
    ]
    rows = [
        [
            colorize(status_color(r["status"]), r["status"]),
            str(r["age"]),
            r["id"],
            r["priority"],
            r["project"],
            r["parent"],
            str(r["comments"]),
            clip_runes(r["title"], _GROOM_TITLE_CLIP),
        ]
        for r in records
    ]
    if rows:
        print_table(rows, headers)
    else:
        scope = f' in project "{project}"' if project else ""
        print(f"(no non-done issues{scope})")

    # footer: data-health summary (reuse validate) + the non-done total. Validate
    # always runs over the WHOLE repo (data integrity isn't project-scoped).
    problems = collect_validation_problems(all_issues)
    print()
    if problems:
        print(
            f"validate: {len(problems)} problem(s) across {len(all_issues)} issue(s) "
            "— run `docket validate` for detail"
        )
    else:
        print(f"validate: OK — {len(all_issues)} issue(s) clean")
    print(f"non-done: {len(records)}")

    # By-construction writing-health signals: unfilled skeleton sections +
    # comment-length distribution. Text footer only — the --json records keep
    # their per-issue shape (agents consume those).
    health = _writing_health(issues, find_repo_root())
    if health["unfilled_issues"]:
        print(
            f"骨架未填: {health['unfilled_issues']} 条 issue 共 "
            f"{health['unfilled_sections']} 个 section 仍是占位提示"
        )
    if health["comment_count"]:
        print(
            f"comment 长度(字): p50={health['comment_p50']} "
            f"p95={health['comment_p95']} max={health['comment_max']} · "
            f"warn(>{_COMMENT_WARN_CHARS}): {health['comment_warn']} 条 · "
            f"alarm(>{_COMMENT_ALARM_CHARS}): {health['comment_alarm']} 条"
        )
        if health["comment_warn"] or health["comment_alarm"]:
            print(
                "长 comment 提示: 长 artifact 落 KB/docs/artifact;"
                "comment 只留链接 + 一句结论"
            )
    long_health = _long_work_health_from_signals(
        _work_health(issues, projects, project=project)
    )
    if long_health["candidates"]:
        hints = long_health["hints"]
        print(
            f"长期工作健康: 候选 {long_health['candidates']} 条 · "
            f"结构提示 {len(hints)} 条"
        )
        if hints:
            preview = "; ".join(
                f"{h['id']}({', '.join(h['hints'])})" for h in hints[:5]
            )
            more = "" if len(hints) <= 5 else f" · 另 {len(hints) - 5} 条"  # noqa: PLR2004
            print(f"长期工作提示: {preview}{more}")


# ---- orphans (『提交了没关单』detector) ----

#: Title / commit-subject clip widths for the orphans table (code points).
_ORPHAN_TITLE_CLIP = 40
_ORPHAN_SUBJ_CLIP = 44


def _known_prefixes(projects) -> set[str]:
    """Issue-id prefixes worth matching in commit messages: every registered
    project's display prefix plus the canonical id prefix. Restricting to REAL
    prefixes is what keeps non-issue tokens out of the match set — the number is
    the true anchor, so a naive `[A-Z]+-\\d+` would let UTF-8 / SHA-256 / ADR-008
    normalize onto whatever issue shares that number."""
    prefixes = {id_prefix()}
    for p in projects.values():
        if p.prefix:
            prefixes.add(p.prefix)
    return prefixes


def _issue_ref_re(prefixes) -> re.Pattern:
    """Word-boundaried, case-insensitive `<prefix>-<number>` matcher over the
    known-prefix set (longest first so a longer prefix wins on overlap)."""
    alt = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    return re.compile(rf"\b({alt})-(\d+)\b", re.IGNORECASE)


def _scan_commit_refs(records, ref_re) -> dict[str, list[tuple[str, str]]]:
    """Map canonical issue id -> [(short_hash, subject), ...] for every commit
    whose message (subject + body) references that id. Deduped per commit, so a
    commit naming the same issue twice counts once."""
    refs: dict[str, list[tuple[str, str]]] = {}
    for h, subj, body in records:
        seen = {
            normalize_id(f"{m.group(1)}-{m.group(2)}")
            for m in ref_re.finditer(f"{subj}\n{body}")
        }
        for cid in seen:
            refs.setdefault(cid, []).append((h, subj))
    return refs


def _orphan_records(refs, by_id, projects) -> list[dict]:
    """One record per referenced issue that is still OPEN (state_type ∉
    {completed, canceled} and not an un-accepted triage proposal). Referenced ids
    that don't exist are skipped — a typo'd ref isn't an orphan. Sorted by commit
    count desc, then id."""
    records = []
    for cid, commits in refs.items():
        is_ = by_id.get(cid)
        if is_ is None:
            continue
        if is_.state_type() in _CLOSED_STATES:
            continue  # already closed → not an orphan (this is the happy path)
        if is_.is_triage():
            continue  # un-accepted proposal isn't real open work (ADR-008)
        records.append(
            {
                "id": display_id(is_, projects),
                "status": is_.status(),
                "commits": [{"hash": h, "subject": s} for h, s in commits],
                "n_commits": len(commits),
                "title": is_.title(),
                "_n": id_num(is_.id())[0],
            }
        )
    records.sort(key=lambda r: (-r["n_commits"], r["_n"]))
    for r in records:
        del r["_n"]
    return records


def cmd_orphans(repo="", limit=200, as_json=False):
    """Cross-check a code repo's recent commits against docket's OPEN issues to
    catch『提交了没关单』: a commit whose message references an issue id (e.g.
    "fix(x): … (DEMO-643)") while that issue is still open — the work-closeout
    Git gate, automated (bd doctor orphan 类比). Read-only: never touches state.

    repo defaults to the current directory (the code repo you just committed in);
    --repo scans another path. The open-issue set comes from the docket data repo
    ($DOCKET_ROOT), decoupled from --repo. --limit caps how many recent commits
    are scanned (default 200) so this stays a『最近提交』check, not a full-history
    sweep. Only real project prefixes (+ the canonical id prefix) are matched, so
    non-issue tokens (UTF-8 / SHA-256 / ADR-008) never false-positive. --json
    emits the records for agent fan-out.

    Non-goal (v0): the reverse — open issues with NO commit at all (空转) — which
    needs a full-history scan and is far noisier (backlog items legitimately have
    none). Left for a later pass."""
    repo = repo.strip() or str(Path.cwd())
    issues = load_all()
    projects, _ = load_projects()
    by_id = {is_.id(): is_ for is_ in issues}
    ref_re = _issue_ref_re(_known_prefixes(projects))
    records = git_log_records(repo, limit)
    orphans = _orphan_records(_scan_commit_refs(records, ref_re), by_id, projects)

    if as_json:
        print(json.dumps(orphans, ensure_ascii=False, indent=2))
        return

    if not orphans:
        print(f"orphans: none — 扫了 {len(records)} 个 commit,无『提交了没关单』")
        return

    headers = ["STATUS", "ID", "#", "LAST COMMIT", "TITLE"]
    rows = []
    for r in orphans:
        last = r["commits"][0]  # newest first (git log order)
        rows.append(
            [
                colorize(status_color(r["status"]), r["status"]),
                r["id"],
                str(r["n_commits"]),
                f"{last['hash']} {clip_runes(last['subject'], _ORPHAN_SUBJ_CLIP)}",
                clip_runes(r["title"], _ORPHAN_TITLE_CLIP),
            ]
        )
    print_table(rows, headers)
    print()
    print(
        f"orphans: {len(orphans)} 个 open issue 被最近 {len(records)} 个 commit "
        "引用但未关单 — 复核后 `docket finish <id>` 或补 comment"
    )
