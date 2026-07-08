"""Project-layer views: the projects/<key>.md container, plus the human-facing
read commands built on it (projects / tree / overview / project). Port of
projects.go.

A Project is a mid-level container (projects/<key>.md): a few-days-to-weeks
effort with deliverables. Issues belong to one by setting their `project` field
to the project's key. The prefix is a DISPLAY label only — the canonical id stays
ISSUE-N (the number is the true anchor), so an issue can be reassigned to another
project (a different prefix) without its id or any reference ever breaking.

These are read-only views: no git/telemetry side effects here.
"""

from pathlib import Path

from .errors import DocketError
from .issue import (
    find_repo_root,
    id_num,
    id_prefix,
    load_all,
    normalize_id,
    parse_issue,
    sort_by_priority,
    today,
    unquote_scalar,
)
from .render import (
    C_BOLD,
    C_CYAN,
    C_DIM,
    C_GRAY,
    C_GREEN,
    C_YELLOW,
    clip_runes,
    colorize,
    display_width,
    pad,
    print_table,
    progress_bar,
    status_color,
)


class Project:
    """A mid-level container (projects/<key>.md)."""

    __slots__ = ("body", "key", "lane", "path", "prefix", "rank", "status", "title")

    def __init__(  # noqa: PLR0913
        self,
        key="",
        title="",
        prefix="",
        status="",
        body="",
        path="",
        lane="",
        rank="",
    ):  # plain data container: one arg per projects/<key>.md field
        self.key = key
        self.title = title
        self.prefix = prefix
        self.status = status
        self.body = body
        self.path = path
        self.lane = lane
        self.rank = rank


def project_work_lane(lane) -> bool:
    """Return whether a project belongs to the work-first lane.

    Empty or unknown values stay in the non-work lane so old project files remain
    compatible and humans opt in only the projects that should interrupt first.
    """
    return str(lane or "").strip().lower() == "work"


def project_lane_label(p) -> str:
    return "工作项目" if project_work_lane(p.lane) else "非工作项目"


def project_rank(p) -> int:
    try:
        return int(str(p.rank or "").strip())
    except ValueError:
        return 1000


def sort_projects_work_first(projects, active_count):
    """Order projects by lane, optional rank, active work, then file order."""
    return sorted(
        projects,
        key=lambda p: (
            0 if project_work_lane(p.lane) else 1,
            project_rank(p),
            -active_count(p.key),
        ),
    )


def project_archived(status) -> bool:
    """Report whether a project is hidden from the default `projects` view (only
    --all shows these). Mirrors issue archiving."""
    return status.strip().lower() in ("done", "archived", "canceled", "cancelled")


def load_projects():
    """Read projects/*.md into a key->Project map plus a list ordered by file
    name. A missing projects/ dir is NOT an error (returns empty) so the rest of
    docket degrades gracefully to the ISSUE- prefix before any project files exist;
    unparseable project files are skipped silently."""
    by_key = {}
    ordered = []
    try:
        root = find_repo_root()
    except DocketError:
        return by_key, ordered
    paths = sorted(str(p) for p in (Path(root) / "projects").glob("*.md"))
    for p in paths:
        try:
            is_ = parse_issue(p)  # generic frontmatter+body parser, reused
        except Exception:
            continue  # Go's loadProjects swallows ALL parse errors and continues
        key = unquote_scalar(get_or(is_, "key")).strip()
        if key == "":
            key = Path(p).stem  # fall back to stem
        proj = Project(
            key=key,
            title=unquote_scalar(get_or(is_, "title")),
            prefix=unquote_scalar(get_or(is_, "prefix")).strip(),
            status=unquote_scalar(get_or(is_, "status")).strip(),
            lane=unquote_scalar(get_or(is_, "lane")).strip(),
            rank=unquote_scalar(get_or(is_, "rank")).strip(),
            body=is_.body,
            path=p,
        )
        by_key[key] = proj
        ordered.append(proj)
    return by_key, ordered


def get_or(is_, key) -> str:
    v, _ = is_.get(key)
    return v


def display_id(is_, projects) -> str:
    """Render an issue's id with its project's prefix (e.g. DEMO-320), falling back
    to the canonical ISSUE-N when the issue has no known project. Only the label
    differs; the number is unchanged."""
    n, ok = id_num(is_.id())
    if not ok:
        return is_.id()
    prefix = id_prefix()
    p = projects.get(is_.project())
    if p is not None and p.prefix != "":
        prefix = p.prefix
    return f"{prefix}-{n}"


def progress_counts(issues) -> tuple[int, int]:
    """Return completed/scope counts for project progress.

    Canceled issues are audit records for work that left scope. They stay visible
    in detailed views, but they should not make a finished project look
    incomplete. Un-accepted triage proposals (ADR-008) are likewise out of scope —
    excluding them keeps the single-project drill (`pm <key>`) and the TUI
    per-project bars on the same 口径 as `cmd_overview` / `docket projects`.
    """
    done = 0
    total = 0
    for is_ in issues:
        if is_.is_triage():
            continue
        st = is_.state_type()
        if st == "canceled":
            continue
        total += 1
        if st == "completed":
            done += 1
    return done, total


def cmd_projects(  # noqa: C901, PLR0912, PLR0915
    all_,
):  # one project-overview render (port of projects.go)
    """Print the project-layer overview: one row per project with its prefix,
    title, status, and progress (active count + done/scope). Default hides
    archived projects (status done/archived/canceled); --all shows them. Issues
    with no project, or whose project key has no projects/<key>.md, are surfaced
    as actionable drift warnings."""
    by_key, ordered = load_projects()
    issues = load_all()

    class _Tally:
        __slots__ = ("active", "done", "total")

        def __init__(self):
            self.active = 0
            self.done = 0
            self.total = 0

    counts = {}

    def tally_for(k):
        t = counts.get(k)
        if t is None:
            t = _Tally()
            counts[k] = t
        return t

    unassigned = []
    unknown_project = []
    for is_ in issues:
        k = is_.project()
        # triage (un-accepted) proposals don't count as active or scope — same
        # 口径 as `cmd_overview` so the two project views agree (ADR-008).
        if is_.is_triage():
            continue
        st = is_.state_type()
        if st == "canceled":
            continue
        if k == "":
            unassigned.append(is_)
            continue
        if k not in by_key:
            unknown_project.append(is_)
            continue
        t = tally_for(k)
        t.total += 1
        if st in ("started", "unstarted"):
            t.active += 1
        elif st == "completed":
            t.done += 1

    # 工作项目优先,组内按可选 rank 排项目优先级,再按当前 active 数降序。
    # 平手保留 load_projects 的文件名序(sorted 稳定);与 overview / pmui 同口径。
    ordered = sort_projects_work_first(
        ordered, lambda key: counts[key].active if key in counts else 0
    )

    rows_by_lane = {}
    for p in ordered:
        if not all_ and project_archived(p.status):
            continue
        t = tally_for(p.key)
        prefix = p.prefix
        if prefix == "":
            prefix = "—"
        rows_by_lane.setdefault(project_lane_label(p), []).append(
            [
                p.key,
                prefix,
                p.title,
                "work" if project_work_lane(p.lane) else "non-work",
                p.status,
                f"{t.active}",
                f"{t.done}/{t.total}",
            ]
        )
    if not rows_by_lane:
        print("(no projects — add projects/<key>.md)")
    else:
        first = True
        for label in ("工作项目", "非工作项目"):
            rows = rows_by_lane.get(label, [])
            if not rows:
                continue
            if not first:
                print()
            first = False
            print(colorize(C_BOLD, label))
            print_table(
                rows,
                [
                    "PROJECT",
                    "PREFIX",
                    "TITLE",
                    "LANE",
                    "STATUS",
                    "ACTIVE",
                    "DONE/SCOPE",
                ],
            )
    if unassigned:
        ids = ", ".join(is_.id() for is_ in unassigned)
        print(f"\n⚠ {len(unassigned)} issue(s) have no project: {ids}")
    if unknown_project:
        pairs = ", ".join(f"{is_.id()} -> {is_.project()}" for is_ in unknown_project)
        print(
            f"\n⚠ {len(unknown_project)} issue(s) reference unknown project key: {pairs}"
        )


def cmd_tree(id_):
    """Print an issue and its descendants (by parent field) as an indented tree,
    using prefixed display ids. Cycle-guarded."""
    root_id = normalize_id(id_)
    issues = load_all()
    by_id = {}
    children = {}
    for is_ in issues:
        by_id[is_.id()] = is_
        p = is_.parent()
        if p not in ("", "~"):
            children.setdefault(p, []).append(is_)
    root = by_id.get(root_id)
    if root is None:
        raise DocketError(f"issue {root_id} not found")
    projects, _ = load_projects()
    seen = set()

    def walk(is_, depth):
        if is_.id() in seen:
            return  # cycle guard
        seen.add(is_.id())
        marker = ""
        if depth > 0:
            marker = "  " * (depth - 1) + "└ "
        print(
            f"{marker}{display_id(is_, projects)}  [{colorize(status_color(is_.status()), is_.status())}]  {is_.title()}"
        )
        kids = children.get(is_.id(), [])
        kids.sort(key=lambda c: id_num(c.id())[0])
        for c in kids:
            walk(c, depth + 1)

    walk(root, 0)


def _print_wake_due_line(issues, by_key):
    """Print a one-line "N 个到期待看 (…)" hint when any snoozed issue has woken
    up (wake ≤ today). No-op when N=0. Mirrors commands.print_wake_due_line but
    stays local to avoid a projects→commands import cycle."""
    due = [is_ for is_ in issues if is_.is_awake_due()]
    if not due:
        return
    sort_by_priority(due)
    ids = ", ".join(display_id(is_, by_key) for is_ in due)
    print(colorize(C_BOLD, f"⏰ {len(due)} 个到期待看") + colorize(C_DIM, f"  {ids}"))
    print()


def _print_triage_pending_line(issues):
    """Print a one-line "⚠ N 条待审 (docket triage)" nag when un-accepted triage
    proposals are pending (un-TTL-expired). No-op when N=0. Mirrors
    commands.print_triage_pending_line but stays local to avoid an import cycle."""
    pend = [is_ for is_ in issues if is_.is_triage_active()]
    if not pend:
        return
    print(colorize(C_BOLD, f"⚠ {len(pend)} 条待审 (docket triage)"))
    print()


def cmd_overview(  # noqa: C901, PLR0912, PLR0915
    show_projects: bool = True,
):  # one whole-state index render (port of overview.go)
    """Print a human-facing one-screen index of the whole PM state: what's in
    progress, then every project's progress bar + a pointer (id→file) to its next
    issue. Issue bodies are intentionally omitted — this is a navigation index
    (glance, then open the file), not a content dump, and it is deterministic so
    it never varies the way an agent's summary would.

    show_projects=False omits the trailing per-project progress section (在飞+本批
    only) — useful when injecting a compact surface into another tool's startup
    context, where the box-drawing project table is the priciest, least-actionable
    part."""
    by_key, ordered = load_projects()
    issues = load_all()

    class _PData:
        __slots__ = ("done", "inprog", "todo", "total")

        def __init__(self):
            self.done = 0
            self.total = 0
            self.inprog = []  # started
            self.todo = []  # unstarted

    data = {}

    def pd(k):
        d = data.get(k)
        if d is None:
            d = _PData()
            data[k] = d
        return d

    in_progress = []
    for is_ in issues:
        d = pd(is_.project())
        st = is_.state_type()
        # triage (un-accepted) proposals are off every work surface and out of the
        # progress denominator — same口径 as overview's stats / `docket projects`.
        if is_.is_triage():
            continue
        if st != "canceled":
            d.total += 1
        if st == "started":
            d.inprog.append(is_)
            # snoozed (wake in the future) issues are hidden from the focus list —
            # they're blocked on something external and can't be pushed now.
            if not is_.is_snoozed():
                in_progress.append(is_)
        elif st == "unstarted":
            d.todo.append(is_)
        elif st == "completed":
            d.done += 1

    print(
        f"{colorize(C_BOLD, 'docket')}   {today()}   {len(issues)} issue · {len(ordered)} 项目\n"
    )

    _print_wake_due_line(issues, by_key)
    _print_triage_pending_line(issues)

    # 在飞: everything in progress, across projects (the real focus list)
    sort_by_priority(in_progress)
    print(colorize(C_BOLD, f"进行中 · {len(in_progress)}"))
    if len(in_progress) == 0:
        print(colorize(C_DIM, "   (无)"))
    for is_ in in_progress:
        print(f"   {pad(colorize(C_CYAN, display_id(is_, by_key)), 9)}  {is_.title()}")
    print()

    # 本批: the lowest open rolling batch — the "now" tranche (cf. docket batch).
    # Snoozed (future-wake) todos are hidden — they can't be pulled now.
    batched_todo = [
        is_
        for is_ in issues
        if is_.state_type() == "unstarted"
        and is_.batch() is not None
        and not is_.is_snoozed()
        and not is_.is_triage()
    ]
    cur_batch = min((is_.batch() for is_ in batched_todo), default=None)
    cur_items = (
        [is_ for is_ in batched_todo if is_.batch() == cur_batch]
        if cur_batch is not None
        else []
    )
    sort_by_priority(cur_items)
    label = f"本批 · batch {cur_batch}" if cur_batch is not None else "本批"
    print(colorize(C_BOLD, f"{label} · {len(cur_items)}"))
    if len(cur_items) == 0:
        print(colorize(C_DIM, "   (无)"))
    for is_ in cur_items:
        print(
            f"   {pad(colorize(C_YELLOW, display_id(is_, by_key)), 9)}  {is_.title()}"
        )
    print()

    if not show_projects:
        return

    # 项目: full map — progress + a pointer to the next issue per project
    ordered = sort_projects_work_first(
        ordered, lambda key: len(pd(key).inprog) + len(pd(key).todo)
    )
    key_w = 0
    title_w = 0
    for p in ordered:
        w = display_width(p.key)
        key_w = max(key_w, w)
        w = display_width(p.title)
        title_w = max(title_w, w)
    print(colorize(C_BOLD, "项目"))
    current_label = ""
    for p in ordered:
        label = project_lane_label(p)
        if label != current_label:
            current_label = label
            print(f"   {colorize(C_BOLD, label)}")
        d = pd(p.key)
        dot = colorize(C_GRAY, "○")
        if len(d.inprog) > 0:
            dot = colorize(C_CYAN, "●")
        elif d.total > 0 and d.done == d.total:
            dot = colorize(C_GREEN, "✓")
        elif len(d.todo) > 0:
            dot = colorize(C_YELLOW, "○")
        # next pointer: an in-progress issue, else the top-priority todo
        next_ = None
        if len(d.inprog) > 0:
            next_ = d.inprog[0]
        elif len(d.todo) > 0:
            sort_by_priority(d.todo)
            next_ = d.todo[0]
        ptr = ""
        if next_ is not None:
            iid = colorize(status_color(next_.status()), display_id(next_, by_key))
            ptr = colorize(C_DIM, "▸ ") + iid + " " + clip_runes(next_.title(), 22)
            extra = len(d.inprog) + len(d.todo) - 1
            if extra > 0:
                ptr += colorize(C_DIM, f"  (+{extra})")
        print(
            f"   {dot} {pad(p.key, key_w)}  {pad(p.title, title_w)}  {progress_bar(d.done, d.total, 12)}  {d.done:2d}/{d.total:<2d}   {ptr}"
        )


def cmd_project(key):
    """Drill one project (singular): header (title/prefix/status + progress), the
    project's plan/milestones (its body), then its issues grouped by status.
    Colored. This is the project-level human view (cf. cmd_overview = all
    projects, cmd_show = one issue)."""
    by_key, _ = load_projects()
    p = by_key.get(key)
    if p is None:
        raise DocketError(f'project "{key}" not found (see `docket projects` for keys)')
    issues = load_all()
    mine = []
    for is_ in issues:
        if is_.project() == key:
            mine.append(is_)
    done, total = progress_counts(mine)

    prefix = p.prefix
    if prefix == "":
        prefix = "—"
    print(
        f"{colorize(C_BOLD, p.title)}  {colorize(C_DIM, '(' + prefix + ')')}  {colorize(C_DIM, p.status)}  {progress_bar(done, total, 16)}  {done}/{total}"
    )
    body = p.body.rstrip("\n")
    if body.strip() != "":
        print(f"\n{body}")

    # issues grouped by state, in working order
    groups = [
        ("started", "进行中"),
        ("unstarted", "待办"),
        ("backlog", "Backlog"),
        ("completed", "已完成"),
        ("canceled", "已取消"),
    ]
    for st, label in groups:
        bucket = [is_ for is_ in mine if is_.state_type() == st and not is_.is_triage()]
        if len(bucket) == 0:
            continue
        sort_by_priority(bucket)
        print(f"\n{colorize(C_BOLD, label)} · {len(bucket)}")
        for is_ in bucket:
            print(
                f"   {pad(colorize(status_color(is_.status()), display_id(is_, by_key)), 10)}  {is_.title()}"
            )
