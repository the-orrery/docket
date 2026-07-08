"""`docket ui` — 只读交互浏览器(Textual)。

两个 Tab,各为左选择器 + 右侧上下排布:
- 右上:issue list 与 relations pane 左右并排,同高。
- 右下:issue meta + 正文 + 讨论。
- 批次:进行中 / 本批 / 下批 / 后续 / 暂存 / 全部 六个跨项目桶(默认 Tab);末桶「全部」
  把所有 open(进行中 + 待办 + 暂存)一把列全、无视 batch、snoozed 标「💤 睡到 X」。
- 项目:project → issue(按状态分组)。高亮某项目时,右侧详情面板渲该项目自身 body
  (现状段领先);高亮某条 issue 再切回 issue 正文。
复用 docket 真 loader(projects.load_projects / issue.load_all / commands.read_comments /
commands.todo_batches),所见即 `docket project` / `docket batch` / `docket show` 同源,不重新解析。

中栏二级分组:批次 tab 按项目分(「全部」桶除外,它平铺列全),项目 tab 把「待办」拆成 本批/下批/后续。
键:1 批次 · 2 项目 · 3 概览 · ↑↓/jk 移动 · Tab 切面板 · / 搜索 · o 打开 · y 复制id · r 刷新 ·
   [ 后退 · ] 前进 · relations pane 回车跳转 ·
   , . 调左栏宽 · - = 调列表高 · b 边框(lines/boxed)· t 换主题 · p 命令面板 · q 退出。
"""

import contextlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import ClassVar

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
)

from .commands import read_comments, todo_batches
from .issue import id_num, load_all, normalize_id, sort_by_priority
from .projects import (
    display_id,
    load_projects,
    progress_counts,
    sort_projects_work_first,
)
from .render import clip_runes
from .states import PRIORITY_RANK

PRIO = {"Urgent": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🔵"}

# 批次 tab 的固定六桶:进行中 / 本批 / 下批 / 后续 / 暂存 / 全部(见 _build_buckets)。
N_BUCKETS = 6


def _bar(done, total, width=10):
    if total <= 0:
        return " " * width
    filled = round(width * done / total)
    return "█" * filled + "░" * (width - filled)


def _bar_text(width, ratio, color, track="grey30"):
    """Rich 单色进度条:filled 着色 + track 暗。"""
    filled = max(0, min(width, round(width * ratio)))
    t = Text()
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style=track)
    return t


def _selftest_screenshot_path(name: str) -> str:
    return str(Path(tempfile.gettempdir()) / name)


def _stack_text(width, segs):
    """Rich 堆叠条:segs=[(count, color)],按占比连续着色填满 width(round 误差给最大段)。"""
    total = sum(c for c, _ in segs) or 1
    widths = [round(width * c / total) for c, _ in segs]
    if segs:
        mx = max(range(len(segs)), key=lambda i: segs[i][0])
        widths[mx] = max(0, widths[mx] + (width - sum(widths)))
    t = Text()
    for (_c, color), w in zip(segs, widths, strict=False):
        t.append("█" * w, style=color)
    return t


def _project_body_for_panel(body):
    """Reorder a project body so its live `## 现状` section leads the panel.

    项目 body 里「## 现状 (日期)」是抗漂移的活脉搏(到哪/下一步/parked),charter/
    里程碑/「现状·历史」是背景。浏览项目时现状才是重点,所以把现状段抽到最前、其余
    段保序跟随。「## 现状·历史」是历史档,标题前缀虽同但归入背景段(留在原处)。
    无现状段就原样返回 body(只 strip)。
    """
    text = (body or "").strip()
    if "## 现状" not in text:
        return text
    # 按二级标题切段:每段 = 标题行 + 到下一个 `## ` 之前的正文;首段(前导,可能无标题)单列。
    segments = []  # [(is_live_status, "\n".join(seg_lines))]
    cur = []
    cur_is_status = False
    for line in text.split("\n"):
        if line.startswith("## "):
            if cur:
                segments.append((cur_is_status, "\n".join(cur)))
            head = line[3:].strip()
            cur = [line]
            cur_is_status = head.startswith("现状") and not head.startswith("现状·历史")
        else:
            cur.append(line)
    if cur:
        segments.append((cur_is_status, "\n".join(cur)))
    lead = [seg for is_status, seg in segments if is_status]
    rest = [seg for is_status, seg in segments if not is_status]
    return "\n\n".join(s.strip() for s in [*lead, *rest] if s.strip())


def _prefs_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return str(Path(base) / "docket" / "ui.json")


def _load_prefs():
    """左栏宽(列)· issue list 占右侧高 % · 边框样式(lines/boxed)· 主题。缺省 22/50/lines/gruvbox。"""
    lw, lh, st, th = 22, 50, "lines", "gruvbox"
    try:
        with Path(_prefs_path()).open(encoding="utf-8") as f:
            d = json.load(f)
        lw = max(12, min(50, int(d.get("left_w", lw))))
        lh = max(15, min(85, int(d.get("list_h", lh))))
        st = d.get("style", st)
        if st not in ("lines", "boxed"):
            st = "lines"
        th = d.get("theme", th)  # 旧 prefs 无 theme → 默认 gruvbox
    except Exception:
        pass
    return lw, lh, st, th


def _save_prefs(left_w, list_h, style, theme):
    try:
        p = _prefs_path()
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        with Path(p).open("w", encoding="utf-8") as f:
            json.dump(
                {"left_w": left_w, "list_h": list_h, "style": style, "theme": theme}, f
            )
    except Exception:
        pass


class ProjItem(ListItem):
    def __init__(self, project, label):
        super().__init__(Label(label))
        self.project = project


class BucketItem(ListItem):
    def __init__(self, issues, label, all_open=False):
        super().__init__(Label(label))
        self.issues = issues
        # 「全部」桶:中栏不按项目分组,改走「列全 open」渲染(无视 batch, snoozed 标 💤)。
        self.all_open = all_open


class IssueItem(ListItem):
    def __init__(self, issue, label):
        super().__init__(Label(label))
        self.issue = issue


class RelationItem(ListItem):
    def __init__(self, issue, label):
        super().__init__(Label(label))
        self.issue = issue


class ContextItem(ListItem):
    def __init__(self, label):
        super().__init__(Label(label, classes="hdr"))
        self.disabled = True


class PMUI(App):
    CSS = """
    TabbedContent { height: 1fr; }
    .leftlist  { width: 22; }
    .rightcol  { width: 1fr; }
    .toprow    { height: 50%; }
    .issuelist { width: 1fr; }
    .relationslist { width: 1fr; }
    .detail    { height: 50%; padding: 0 1; }
    .meta      { padding: 0 0 1 0; color: $text; text-style: bold; }
    .comments  { padding: 1 0 0 0; color: $text-muted; border-top: solid $primary-darken-2; }
    .lines.leftlist  { border-right: solid $primary-darken-2; }
    .lines.toprow { border-bottom: solid $primary-darken-2; }
    .lines.issuelist { border-right: solid $primary-darken-2; }
    .lines.leftlist:focus-within  { border-right: solid $accent; }
    .lines.issuelist:focus-within { border-right: solid $accent; }
    .lines.relationslist:focus-within { border: solid $accent; }
    .boxed.leftlist  { border: round $primary; }
    .boxed.issuelist { border: round $primary 50%; }
    .boxed.relationslist { border: round $primary 50%; }
    .boxed.detail    { border: round $accent; }
    .boxed.leftlist:focus-within, .boxed.issuelist:focus-within, .boxed.relationslist:focus-within { border: round $accent; }
    ListView { background: transparent; }
    .hdr { color: $text-muted; text-style: bold; }
    .statscol { padding: 0 2; }
    #search { display: none; dock: top; height: 3; }
    """
    BINDINGS: ClassVar[list] = [
        Binding("q", "quit", "退出"),
        Binding("1", "tab('tab-batch')", "批次"),
        Binding("2", "tab('tab-proj')", "项目"),
        Binding("3", "tab('tab-stats')", "概览"),
        Binding("tab", "focus_next", "切面板"),
        Binding("/", "search", "搜索"),
        Binding("o", "open", "打开"),
        Binding("y", "copy", "复制id"),
        Binding("r", "reload", "刷新"),
        Binding("[", "back", "后退"),
        Binding("]", "forward", "前进"),
        Binding("period", "wider", "左栏+"),
        Binding("comma", "narrower", "左栏-"),
        Binding("=", "list_taller", "列表高+"),
        Binding("-", "list_shorter", "列表高-"),
        Binding("b", "toggle_style", "边框"),
        Binding("t", "cycle_theme", "换主题"),
        Binding("p", "command_palette", "命令"),
    ]
    TITLE = "docket"
    SUB_TITLE = "交互浏览 · 只读"

    def __init__(self, *, roots=None):
        super().__init__()
        self.roots = roots  # 留给 action_reload:多 tier 启动时刷新需重走 _load_multi
        if roots:
            self.by_key, self.ordered, self.issues = self._load_multi(roots)
        else:
            self.by_key, self.ordered = load_projects()
            self.issues = load_all()
        self.by_project = {}
        for is_ in self.issues:
            self.by_project.setdefault(is_.project(), []).append(is_)
        self._build_index()
        self._sort_projects_by_activity()
        self.buckets = self._build_buckets()
        self._last_detail = {}
        self.current_issue_id = None
        self.nav_back = []
        self.nav_forward = []
        # 可调(, . 左栏宽 / - = 列表高 / b 边框 / t 主题),落盘到用户配置目录,跨启动记忆。
        self.left_w, self.list_h, self.style_mode, self.theme_name = _load_prefs()

    @staticmethod
    def _load_multi(roots):
        import os

        all_issues = []
        by_key = {}
        ordered = []
        for _tier_name, path in roots:
            os.environ["DOCKET_ROOT"] = path
            try:
                tk, to = load_projects()
                by_key.update(tk)
                ordered.extend(to)
                all_issues.extend(load_all())
            except Exception:
                pass
        os.environ.pop("DOCKET_ROOT", None)
        from .issue import id_num

        all_issues.sort(key=lambda is_: id_num(is_.id())[0])
        return by_key, ordered, all_issues

    def _build_index(self):
        # id→issue 反查 + 父→子映射(详情里渲染父链接 / 子任务清单)。
        # child_idx 不能叫 children:Textual App.children 是只读 property,赋值会抛。
        self.by_id = {i.id(): i for i in self.issues}
        self.child_idx = {}
        for i in self.issues:
            p = self._parent_id(i)
            if p:
                self.child_idx.setdefault(p, []).append(i)

    def _active_count(self, key):
        # 同 `docket projects` 的 ACTIVE 列口径:进行中 + 待办(canceled 不计)。
        return sum(
            1
            for i in self.by_project.get(key, [])
            if i.state_type() in ("started", "unstarted") and not i.is_triage()
        )

    def _sort_projects_by_activity(self):
        # 项目左栏(及批次中栏分组)按工作项目优先,组内按当前 active 数稳定降序。
        # 平手保留 load_projects 的文件名序。需在 by_project 建好后调用。
        self.ordered = sort_projects_work_first(self.ordered, self._active_count)

    def _build_buckets(self):
        """跨项目六桶:进行中 / 本批 / 下批 / 后续 / 暂存 / 全部。一次性算好(同 batch
        view)。末尾「全部」是所有 open(进行中+待办+暂存)一把列全的入口(all_open=True
        标记 → 中栏走「列全 open」渲染而非按项目分组)。"""
        # triage (un-accepted) proposals are off every work bucket; they live in
        # their own 「待审」桶 (active = un-TTL-expired) until accept/decline (ADR-008).
        started = [
            i for i in self.issues if i.state_type() == "started" and not i.is_triage()
        ]
        backlog = [
            i for i in self.issues if i.state_type() == "backlog" and not i.is_triage()
        ]
        todo = [
            i
            for i in self.issues
            if i.state_type() == "unstarted" and not i.is_triage()
        ]
        triage = [i for i in self.issues if i.is_triage_active()]
        nums = todo_batches(self.issues)
        cur = nums[0] if nums else None
        nxt = nums[1] if len(nums) > 1 else None
        self.cur_batch, self.nxt_batch = cur, nxt  # 项目 tab 的待办分层复用
        cur_items, nxt_items, rest = [], [], []
        for is_ in todo:
            b = is_.batch()
            if cur is not None and b == cur:
                cur_items.append(is_)
            elif nxt is not None and b == nxt:
                nxt_items.append(is_)
            else:
                rest.append(is_)
        cur_label = "本批" + (f" · batch {cur}" if cur is not None else "")
        nxt_label = "下批" + (f" · batch {nxt}" if nxt is not None else "")
        all_open = started + todo + backlog
        # tuple = (label, items, all_open?);前五桶 all_open=False,末桶「全部」=True。
        return [
            ("进行中", started, False),
            (cur_label, cur_items, False),
            (nxt_label, nxt_items, False),
            ("后续", rest, False),
            ("暂存", backlog, False),
            ("待审", triage, False),
            ("全部", all_open, True),
        ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="/ 搜 id 或标题(Esc 退出)", id="search")
        with TabbedContent(initial="tab-batch"):
            with TabPane("批次", id="tab-batch"), Horizontal():
                yield ListView(id="batch-left", classes="leftlist")
                with Vertical(classes="rightcol"):
                    with Horizontal(classes="toprow"):
                        yield ListView(id="batch-issues", classes="issuelist")
                        yield ListView(id="batch-relations", classes="relationslist")
                    with VerticalScroll(classes="detail"):
                        yield Static("← 选桶 / issue", id="batch-meta", classes="meta")
                        yield Markdown("", id="batch-md")
                        yield Markdown("", id="batch-comments", classes="comments")
            with TabPane("项目", id="tab-proj"), Horizontal():
                yield ListView(id="proj-left", classes="leftlist")
                with Vertical(classes="rightcol"):
                    with Horizontal(classes="toprow"):
                        yield ListView(id="proj-issues", classes="issuelist")
                        yield ListView(id="proj-relations", classes="relationslist")
                    with VerticalScroll(classes="detail"):
                        yield Static(
                            "← 选 project / issue", id="proj-meta", classes="meta"
                        )
                        yield Markdown("", id="proj-md")
                        yield Markdown("", id="proj-comments", classes="comments")
            with TabPane("概览", id="tab-stats"):  # noqa: SIM117 — Textual compose: TabPane and VerticalScroll are distinct widget layers
                with VerticalScroll(classes="statscol"):
                    yield Static(id="stats-body")
        yield Footer()

    async def _populate_left(self):
        """填充两个左栏(批次六桶 + 项目),清空后重填。on_mount / action_reload 共用。"""
        # 批次 tab:左栏填六桶(末桶「全部」带 all_open 标记)
        bl = self.query_one("#batch-left", ListView)
        await bl.clear()
        for label, items, all_open in self.buckets:
            bl.append(
                BucketItem(items, f"{label}\n  · {len(items)}", all_open=all_open)
            )
        # 项目 tab:左栏填项目(前缀 + 进度条 + done/scope)
        pl = self.query_one("#proj-left", ListView)
        await pl.clear()
        for p in self.ordered:
            mine = self.by_project.get(p.key, [])
            done, total = progress_counts(mine)
            prefix = p.prefix or p.key
            pl.append(ProjItem(p, f"{prefix}\n{_bar(done, total)} {done}/{total}"))

    async def on_mount(self):
        with contextlib.suppress(Exception):
            self.theme = self.theme_name
        await self._populate_left()
        bl = self.query_one("#batch-left", ListView)
        pl = self.query_one("#proj-left", ListView)
        bl.focus()
        if self.buckets:
            bl.index = 0
            await self._load_bucket(self.buckets[0][1])
        if self.ordered:
            pl.index = 0
            await self._load_project(self.ordered[0].key)
        self._render_stats()  # 概览 tab(部件已存在)
        self._apply_layout()  # 套用记忆的比例(可能与 CSS 缺省不同)
        self._apply_style()

    def _add_header(self, lv, text):
        hdr = ListItem(Label(f"— {text}", classes="hdr"))
        hdr.disabled = True
        lv.append(hdr)

    def _tree_sort_key(self, is_):
        return (PRIORITY_RANK.get(is_.priority(), 99), id_num(is_.id())[0])

    def _parent_id(self, is_):
        pid = is_.parent()
        if not pid or pid == "~":
            return ""
        return normalize_id(pid)

    def _child_summary(self, is_):
        kids = self.child_idx.get(is_.id()) or []
        if not kids:
            return ""
        live = sum(1 for c in kids if c.state_type() not in ("completed", "canceled"))
        done, total = progress_counts(kids)
        return f" · 子 {done}/{total}" if live else f" · 子 {total}"

    def _row_prefix(self, depth):
        return ("  " * min(depth, 5)) + ("↳ " if depth else "")

    def _add_issue(self, lv, is_, depth=0):
        mark = PRIO.get(is_.priority(), "  ")
        did = display_id(is_, self.by_key)
        label = (
            f"{self._row_prefix(depth)}{mark} {did}  "
            f"{clip_runes(is_.title(), 58)}{self._child_summary(is_)}"
        )
        lv.append(IssueItem(is_, label))

    def _add_context(self, lv, is_, depth=0):
        did = display_id(is_, self.by_key)
        label = f"{self._row_prefix(depth)}↥ {did}  {clip_runes(is_.title(), 58)}"
        lv.append(ContextItem(label))

    def _add_issue_tree(  # noqa: C901
        self, lv, issues
    ):  # one tree-build pass; splitting fragments the walk
        """Render the current filtered group as a parent/child tree.

        Ancestors outside the filter are shown as disabled context rows, so the
        group membership remains truthful while the path is still visible.
        """
        group = {i.id(): i for i in issues}
        display_ids = set(group)
        for is_ in issues:
            seen = set()
            pid = self._parent_id(is_)
            while pid and pid in self.by_id and pid not in seen:
                display_ids.add(pid)
                seen.add(pid)
                pid = self._parent_id(self.by_id[pid])

        roots = []
        children = {}
        for issue_id in display_ids:
            is_ = self.by_id.get(issue_id)
            if is_ is None:
                continue
            pid = self._parent_id(is_)
            if pid and pid in display_ids:
                children.setdefault(pid, []).append(is_)
            else:
                roots.append(is_)

        roots.sort(key=self._tree_sort_key)
        for vals in children.values():
            vals.sort(key=self._tree_sort_key)

        seen = set()

        def visit(is_, depth=0):
            if is_.id() in seen:
                return
            seen.add(is_.id())
            if is_.id() in group:
                self._add_issue(lv, is_, depth)
            else:
                self._add_context(lv, is_, depth)
            for child in children.get(is_.id(), []):
                visit(child, depth + 1)

        for root in roots:
            visit(root)

    async def _load_bucket(self, issues):
        # 批次桶 → 中栏按项目分组(批次 → 项目 → issue)。
        lv = self.query_one("#batch-issues", ListView)
        await lv.clear()
        pool = list(issues)
        known = {p.key for p in self.ordered}
        for p in self.ordered:
            grp = [i for i in pool if i.project() == p.key]
            if not grp:
                continue
            sort_by_priority(grp)
            self._add_header(lv, f"{p.prefix or p.key} ({len(grp)})")
            self._add_issue_tree(lv, grp)
        orphans = [i for i in pool if i.project() not in known]
        if orphans:
            sort_by_priority(orphans)
            self._add_header(lv, f"(其他) ({len(orphans)})")
            self._add_issue_tree(lv, orphans)

    async def _load_project(self, key):
        # 项目 → 中栏按状态分组,且把「待办」拆成 本批/下批/后续(项目 → 批次 → issue)。
        # 同时把项目自身 body(现状段领先)渲进右侧详情面板,清掉上一个 issue 的残留。
        await self._show_project_detail(key)
        lv = self.query_one("#proj-issues", ListView)
        await lv.clear()
        mine = [i for i in self.by_project.get(key, []) if not i.is_triage()]
        cur, nxt = self.cur_batch, self.nxt_batch
        todo = [i for i in mine if i.state_type() == "unstarted"]
        ben = [i for i in todo if cur is not None and i.batch() == cur]
        xia = [i for i in todo if nxt is not None and i.batch() == nxt]
        hou = [i for i in todo if i not in ben and i not in xia]
        groups = [
            ("进行中", [i for i in mine if i.state_type() == "started"]),
            ("本批" + (f" · b{cur}" if cur is not None else ""), ben),
            ("下批" + (f" · b{nxt}" if nxt is not None else ""), xia),
            ("后续", hou),
            ("暂存", [i for i in mine if i.state_type() == "backlog"]),
            ("已完成", [i for i in mine if i.state_type() == "completed"]),
            ("已取消", [i for i in mine if i.state_type() == "canceled"]),
        ]
        for label, grp in groups:
            if not grp:
                continue
            sort_by_priority(grp)
            self._add_header(lv, f"{label} ({len(grp)})")
            self._add_issue_tree(lv, grp)

    async def _show_project_detail(self, key):
        # 项目高亮 → 右侧详情面板渲项目自身脉搏(而非上一个 issue 残留)。
        # 现状段领先(_project_body_for_panel),relations/comments 清空,
        # current_issue_id 置 None:浏览项目时不对应某条 issue。
        p = self.by_key.get(key)
        if p is None:
            return
        mine = self.by_project.get(key, [])
        done, total = progress_counts(mine)
        prefix = p.prefix or key
        meta = (
            f"{prefix} · {p.title}\nproject · {p.status or '—'} · {done}/{total} done"
        )
        body = _project_body_for_panel(p.body) or "_(无项目说明)_"
        self.query_one("#proj-meta", Static).update(meta)
        self.query_one("#proj-md", Markdown).update(body)
        self.query_one("#proj-comments", Markdown).update("")
        await self.query_one("#proj-relations", ListView).clear()
        self.current_issue_id = None
        self._last_detail["proj"] = {
            "meta": meta,
            "relations": "",
            "body": body,
            "comments": "",
        }

    async def _load_open(self):
        # 「全部」桶 → 批次中栏列全 open:进行中 + 待办 + 暂存 一把列全,无视 batch;
        # snoozed 标「💤 睡到 X」。复用批次中栏 #batch-issues(详情 prefix 仍是 batch)。
        lv = self.query_one("#batch-issues", ListView)
        await lv.clear()
        groups = [
            (
                "进行中",
                [
                    i
                    for i in self.issues
                    if i.state_type() == "started" and not i.is_triage()
                ],
            ),
            (
                "待办",
                [
                    i
                    for i in self.issues
                    if i.state_type() == "unstarted" and not i.is_triage()
                ],
            ),
            (
                "暂存",
                [
                    i
                    for i in self.issues
                    if i.state_type() == "backlog" and not i.is_triage()
                ],
            ),
        ]
        for label, grp in groups:
            if not grp:
                continue
            sort_by_priority(grp)
            self._add_header(lv, f"{label} ({len(grp)})")
            for is_ in grp:
                self._add_open_issue(lv, is_)

    def _add_open_issue(self, lv, is_):
        # 平铺一行(无树/无缩进):优先级 mark + id + 标题 + snoozed 时「💤 睡到 X」。
        mark = PRIO.get(is_.priority(), "  ")
        did = display_id(is_, self.by_key)
        snooze = f"  💤 睡到 {is_.wake()}" if is_.is_snoozed() else ""
        label = f"{mark} {did}  {clip_runes(is_.title(), 56)}{snooze}"
        lv.append(IssueItem(is_, label))

    def _prefix_for_list(self, lid):
        # issue-中栏 id → 详情面板 prefix(batch / proj)。
        return "batch" if lid == "batch-issues" else "proj"

    async def on_list_view_highlighted(self, event):
        item = event.item
        if item is None:
            return
        lid = event.list_view.id
        if isinstance(item, BucketItem):
            # 「全部」桶走列全 open 渲染;其余批次桶按项目分组。
            if item.all_open:
                await self._load_open()
            else:
                await self._load_bucket(item.issues)
        elif isinstance(item, ProjItem):
            await self._load_project(item.project.key)
        elif isinstance(item, IssueItem):
            await self._show_detail(item.issue, self._prefix_for_list(lid))

    async def on_list_view_selected(self, event):
        item = event.item
        if isinstance(item, RelationItem):
            await self._navigate_to(item.issue, push_history=True)
        elif isinstance(item, IssueItem):
            await self._show_detail(
                item.issue, self._prefix_for_list(event.list_view.id)
            )

    def _relation_header(self, lv, text):
        hdr = ListItem(Label(text, classes="hdr"))
        hdr.disabled = True
        lv.append(hdr)

    def _relation_label(self, is_, prefix=""):
        did = display_id(is_, self.by_key)
        return f"{prefix}{did} · {clip_runes(is_.title(), 62)}"

    async def _render_relations(  # noqa: C901, PLR0912
        self, is_, prefix
    ):  # one relations-panel render (blockers/blocks/parent/children)
        lv = self.query_one(f"#{prefix}-relations", ListView)
        await lv.clear()
        pid = self._parent_id(is_)
        has_any = False
        if pid:
            p = self.by_id.get(pid)
            if p is not None:
                self._relation_header(lv, "父任务")
                lv.append(RelationItem(p, self._relation_label(p, "↑ ")))
                has_any = True
            else:
                self._relation_header(lv, f"父任务: {pid}")
                has_any = True

        kids = self.child_idx.get(is_.id()) or []
        if kids:
            counts = {}
            for c in kids:
                counts[c.status()] = counts.get(c.status(), 0) + 1
            summary = " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            self._relation_header(lv, f"子任务 · {len(kids)} · {summary}")
            for c in sorted(kids, key=self._tree_sort_key):
                lv.append(RelationItem(c, self._relation_label(c, "↳ ")))
            has_any = True

        closed = ("completed", "canceled")
        bb = is_.blocked_by()
        if bb:
            self._relation_header(lv, "被阻塞于")
            for bid in bb:
                b = self.by_id.get(bid)
                if b is None:
                    self._relation_header(lv, f"  {bid} (不存在)")
                else:
                    mark = "✓ " if b.state_type() in closed else "⛔ "
                    lv.append(RelationItem(b, self._relation_label(b, mark)))
            has_any = True
        blocks = [
            o
            for o in self.by_id.values()
            if o.state_type() not in closed and is_.id() in o.blocked_by()
        ]
        if blocks:
            self._relation_header(lv, f"阻塞着 · {len(blocks)}")
            for o in sorted(blocks, key=self._tree_sort_key):
                lv.append(RelationItem(o, self._relation_label(o, "⛔ ")))
            has_any = True

        if not has_any:
            self._relation_header(lv, "关系: —")

    def _active_prefix(self):
        return (
            "batch" if self.query_one(TabbedContent).active == "tab-batch" else "proj"
        )

    def _sync_issue_highlight(self, issue_id):
        _, mid = self._active_ids()
        lv = self.query_one(f"#{mid}", ListView)
        for idx, child in enumerate(lv.children):
            if isinstance(child, IssueItem) and child.issue.id() == issue_id:
                lv.index = idx
                return True
        return False

    async def _navigate_to(self, issue, push_history=False):
        if (
            push_history
            and self.current_issue_id
            and self.current_issue_id != issue.id()
        ):
            self.nav_back.append(self.current_issue_id)
            self.nav_forward.clear()
        prefix = self._active_prefix()
        visible = self._sync_issue_highlight(issue.id())
        await self._show_detail(issue, prefix)
        if not visible:
            self.notify(f"{display_id(issue, self.by_key)} 不在当前列表,仅切换详情")

    async def _show_detail(self, is_, prefix):
        did = display_id(is_, self.by_key)
        batch = is_.batch() if is_.batch() is not None else "—"
        pid = self._parent_id(is_)
        parent_label = "—"
        if pid:
            p = self.by_id.get(pid)
            parent_label = display_id(p, self.by_key) if p is not None else pid
        meta = (
            f"{did} · {is_.title()}\n"
            f"{is_.status()} · {is_.priority()} · project {is_.project()} · "
            f"batch {batch} · parent {parent_label}"
        )

        rel_lines = []
        if pid:
            p = self.by_id.get(pid)
            if p is not None:
                rel_lines.append(f"父任务: {display_id(p, self.by_key)} · {p.title()}")
            else:
                rel_lines.append(f"父任务: {pid}")

        kids = self.child_idx.get(is_.id())
        if kids:
            counts = {}
            for c in kids:
                counts[c.status()] = counts.get(c.status(), 0) + 1
            summary = " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            rel_lines.append(f"子任务: {len(kids)} · {summary}")
            for c in sorted(kids, key=lambda c: id_num(c.id())[0]):
                rel_lines.append(
                    f"  • [{c.status()}] {display_id(c, self.by_key)} · {c.title()}"
                )

        if not rel_lines:
            rel_lines.append("关系: —")

        body = is_.body.strip() or "_(无正文)_"
        # Read comments from the issue's own lane (<lane>/issues/X.md → <lane>),
        # not a global root: multi-tier aggregation pops DOCKET_ROOT and an
        # aggregated issue may belong to a different lane than cwd resolves to.
        lane_root = str(Path(is_.path).resolve().parent.parent)
        content, n = read_comments(is_.id(), root=lane_root)
        comments = ""
        if n:
            comments = f"**讨论 · {n} 条**\n\n{content}"

        self.query_one(f"#{prefix}-meta", Static).update(meta)
        self.query_one(f"#{prefix}-md", Markdown).update(body)
        self.query_one(f"#{prefix}-comments", Markdown).update(comments)
        await self._render_relations(is_, prefix)
        self.current_issue_id = is_.id()
        self._last_detail[prefix] = {
            "meta": meta,
            "relations": "\n".join(rel_lines),
            "body": body,
            "comments": comments,
        }

    def action_tab(self, tab_id: str):
        # 切 tab 同时把焦点移进目标面板:否则旧 tab 里仍获焦的 ListView 会把
        # 它自己的 tab 重新激活回去(focus-within 触发 Tabs 重新选中)。
        self.query_one(TabbedContent).active = tab_id
        if tab_id == "tab-stats":
            # 概览无 ListView,焦点落到滚动容器,否则会被 focus-within 弹回 proj。
            self.query_one(".statscol").focus()
            return
        left = "batch-left" if tab_id == "tab-batch" else "proj-left"
        self.query_one(f"#{left}", ListView).focus()

    def _apply_layout(self):
        for w in self.query(".leftlist"):
            w.styles.width = self.left_w
        for w in self.query(".toprow"):
            w.styles.height = f"{self.list_h}%"
        for w in self.query(".detail"):
            w.styles.height = f"{100 - self.list_h}%"
        # boxed 模式下相邻面板的圆角边框 reflow 后旧边框残留(只在真终端,headless 不复现),
        # 强制整屏 relayout 重绘清掉残影。
        self.refresh(layout=True)

    def _nudge(self, dleft=0, dh=0):
        self.left_w = max(12, min(50, self.left_w + dleft))
        self.list_h = max(15, min(85, self.list_h + dh))
        self._apply_layout()
        _save_prefs(self.left_w, self.list_h, self.style_mode, self.theme_name)
        self.notify(f"左栏 {self.left_w} · 列表 {self.list_h}%(已记忆)")

    def _apply_style(self):
        lines = self.style_mode == "lines"
        for cls in (".leftlist", ".toprow", ".issuelist", ".relationslist", ".detail"):
            for w in self.query(cls):
                w.set_class(lines, "lines")
                w.set_class(not lines, "boxed")

    def action_toggle_style(self):
        self.style_mode = "boxed" if self.style_mode == "lines" else "lines"
        self._apply_style()
        _save_prefs(self.left_w, self.list_h, self.style_mode, self.theme_name)
        self.notify(f"边框:{self.style_mode}(b 切换,已记忆)")

    def action_wider(self):
        self._nudge(dleft=2)

    def action_narrower(self):
        self._nudge(dleft=-2)

    def action_list_taller(self):
        self._nudge(dh=5)

    def action_list_shorter(self):
        self._nudge(dh=-5)

    def action_cycle_theme(self):
        try:
            names = list(self.available_themes.keys())
            i = names.index(self.theme) if self.theme in names else 0
            self.theme = names[(i + 1) % len(names)]
            self.theme_name = self.theme  # 落盘记忆,跨启动恢复
            _save_prefs(self.left_w, self.list_h, self.style_mode, self.theme_name)
            self.notify(f"theme: {self.theme}")
        except Exception as e:
            self.notify(f"theme switch n/a: {e}")

    def _render_stats(self):
        # 概览仪表盘(Rich 着色):完成度 gauge + 状态/批次堆叠条 + 每项目彩色进度条。
        N = len(self.issues)
        M = len(self.ordered)
        # triage (un-accepted) proposals carve out of the work-state segments and
        # show as their own 「待审」count (active = un-TTL-expired) (ADR-008).
        started = sum(
            1 for i in self.issues if i.state_type() == "started" and not i.is_triage()
        )
        unstarted = [
            i
            for i in self.issues
            if i.state_type() == "unstarted" and not i.is_triage()
        ]
        backlog = sum(
            1 for i in self.issues if i.state_type() == "backlog" and not i.is_triage()
        )
        triage_n = sum(1 for i in self.issues if i.is_triage_active())
        completed = sum(1 for i in self.issues if i.state_type() == "completed")
        canceled = sum(1 for i in self.issues if i.state_type() == "canceled")
        cur, nxt = self.cur_batch, self.nxt_batch
        ben = sum(1 for i in unstarted if cur is not None and i.batch() == cur)
        xia = sum(1 for i in unstarted if nxt is not None and i.batch() == nxt)
        hou = len(unstarted) - ben - xia
        scope = N - canceled
        pct = round(100 * completed / scope) if scope else 0
        BW = 46
        STATES = [
            (started, "进行中", "cyan"),
            (len(unstarted), "待办", "yellow"),
            (backlog, "暂存", "blue"),
            (triage_n, "待审", "magenta"),
            (completed, "已完成", "green"),
            (canceled, "已取消", "red"),
        ]
        BATCH = [
            (ben, "本批", "green"),
            (xia, "下批", "yellow"),
            (hou, "后续", "grey50"),
        ]

        title = Text()
        title.append("概览  ", style="bold")
        title.append(f"{N} issue · {M} 项目 · 完成 ", style="dim")
        title.append(f"{pct}%", style="bold green")

        gauge = Text("完成度  ", style="bold")
        gauge.append_text(_bar_text(BW, completed / scope if scope else 0, "green"))
        gauge.append(f"  {completed}/{scope}", style="dim")

        def _row(label, segs, legend_src):
            bar = Text(f"{label}    ", style="bold")
            bar.append_text(_stack_text(BW, [(c, col) for c, _, col in segs]))
            leg = Text("        ")
            for c, name, col in legend_src:
                leg.append("● ", style=col)
                leg.append(f"{name} {c}    ", style="dim")
            return bar, leg

        sbar, sleg = _row("状态", STATES, STATES)
        bbar, bleg = _row("批次", BATCH, BATCH)

        tbl = Table.grid(padding=(0, 2))
        for _ in range(4):
            tbl.add_column()
        for p in self.ordered:
            mine = self.by_project.get(p.key, [])
            done, total = progress_counts(mine)
            r = done / total if total else 0
            col = "green" if r >= 1 else ("cyan" if r > 0 else "grey50")
            tbl.add_row(
                Text(p.prefix or p.key, style="bold"),
                _bar_text(20, r, col),
                Text(f"{done}/{total}", style="dim"),
                Text(f"{round(100 * r)}%", style=col),
            )

        body = Group(
            title,
            Text(),
            gauge,
            Text(),
            sbar,
            sleg,
            Text(),
            bbar,
            bleg,
            Text(),
            Text("每项目", style="bold"),
            tbl,
        )
        self._last_stats = body  # 供 selftest 校验渲染内容
        self.query_one("#stats-body", Static).update(body)

    # ── 当前激活 tab 的部件定位 ──────────────────────────────────────
    def _active_ids(self):
        """按激活 tab 返回 (左栏 id, issue 中栏 id)。"""
        if self.query_one(TabbedContent).active == "tab-batch":
            return "batch-left", "batch-issues"
        return "proj-left", "proj-issues"

    def _current_issue(self):
        """当前详情 issue;无详情时兜底到激活 tab issue 中栏高亮项。"""
        if self.current_issue_id:
            current = self.by_id.get(self.current_issue_id)
            if current is not None:
                return current
        _, mid = self._active_ids()
        item = self.query_one(f"#{mid}", ListView).highlighted_child
        return item.issue if isinstance(item, IssueItem) else None

    # ── / 全局搜索 ──────────────────────────────────────────────────
    def action_search(self):
        inp = self.query_one("#search", Input)
        inp.display = True
        inp.focus()

    async def on_input_changed(self, event):
        if event.input.id != "search":
            return
        _, mid = self._active_ids()
        lv = self.query_one(f"#{mid}", ListView)
        await lv.clear()
        q = event.value.strip().lower()
        if not q:  # 空 query 不 dump 全量
            return
        toks = q.split()
        matches = [
            is_
            for is_ in self.issues
            if all(
                t in f"{display_id(is_, self.by_key)} {is_.title()}".lower()
                for t in toks
            )
        ]
        sort_by_priority(matches)
        self._add_header(lv, f"搜索「{event.value.strip()}」· {len(matches)}")
        for is_ in matches:
            self._add_issue(lv, is_)

    def on_input_submitted(self, event):
        if event.input.id != "search":
            return
        _, mid = self._active_ids()
        self.query_one(f"#{mid}", ListView).focus()

    async def _restore_active_view(self):
        """退出搜索:按激活 tab 左栏高亮项重跑对应 loader,恢复正常中栏。"""
        left, _ = self._active_ids()
        lv = self.query_one(f"#{left}", ListView)
        item = lv.highlighted_child
        if item is None and len(lv.children):  # 无高亮兜底到首项
            lv.index = 0
            item = lv.highlighted_child
        if isinstance(item, BucketItem):
            # 「全部」桶走列全 open;其余批次桶按项目分组。
            await self._load_open() if item.all_open else await self._load_bucket(
                item.issues
            )
        elif isinstance(item, ProjItem):
            await self._load_project(item.project.key)

    async def on_key(self, event):
        # Input 不消费 escape(无对应 binding),冒泡到 App;搜索可见时拦下退出。
        if event.key == "escape":
            inp = self.query_one("#search", Input)
            if inp.display:
                inp.display = False
                inp.value = ""
                left, _ = self._active_ids()
                self.query_one(f"#{left}", ListView).focus()
                await self._restore_active_view()
                event.stop()

    # ── 导航 · o 打开 · y 复制 · r 刷新 ─────────────────────────────
    async def action_back(self):
        if not self.nav_back:
            self.notify("没有后退历史")
            return
        target_id = self.nav_back.pop()
        if self.current_issue_id:
            self.nav_forward.append(self.current_issue_id)
        target = self.by_id.get(target_id)
        if target is None:
            self.notify(f"后退目标不存在: {target_id}")
            return
        await self._navigate_to(target, push_history=False)

    async def action_forward(self):
        if not self.nav_forward:
            self.notify("没有前进历史")
            return
        target_id = self.nav_forward.pop()
        if self.current_issue_id:
            self.nav_back.append(self.current_issue_id)
        target = self.by_id.get(target_id)
        if target is None:
            self.notify(f"前进目标不存在: {target_id}")
            return
        await self._navigate_to(target, push_history=False)

    def action_open(self):
        is_ = self._current_issue()
        if is_ is None:
            self.notify("没选中 issue")
            return
        path = str(Path(is_.path).resolve())
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        try:
            if editor:
                # 终端编辑器(vim 等)需接管整屏 → suspend TUI,退出后自动恢复。
                with self.suspend():
                    subprocess.run([*shlex.split(editor), path])
            else:
                subprocess.run(["open", path])  # macOS 默认 app,不阻塞
            self.notify(f"已在编辑器打开 {is_.id()}")
        except Exception as e:
            self.notify(f"打开失败: {e}")

    def action_copy(self):
        is_ = self._current_issue()
        if is_ is None:
            self.notify("没选中 issue")
            return
        did = display_id(is_, self.by_key)
        try:
            if hasattr(self, "copy_to_clipboard"):
                self.copy_to_clipboard(did)  # OSC52,SSH 下也可用
            else:
                subprocess.run(["pbcopy"], input=did.encode())
            self.notify(f"已复制 {did}")
        except Exception as e:
            self.notify(f"复制失败: {e}")

    async def action_reload(self):
        # 必须跟 __init__ 走同一条加载路径:多 tier 启动时 _load_multi 会把 DOCKET_ROOT
        # 从环境里 pop 掉,这里再走单 tier 的 load_projects/load_all 会因 DOCKET_ROOT 缺失
        # 而 find_repo_root 失败(报 DocketError),也丢掉 work tier 的聚合。
        if self.roots:
            self.by_key, self.ordered, self.issues = self._load_multi(self.roots)
        else:
            self.by_key, self.ordered = load_projects()
            self.issues = load_all()
        self._build_index()
        self.by_project = {}
        for is_ in self.issues:
            self.by_project.setdefault(is_.project(), []).append(is_)
        self._sort_projects_by_activity()
        self.buckets = self._build_buckets()  # 同时重置 cur_batch / nxt_batch
        self.current_issue_id = None
        self.nav_back.clear()
        self.nav_forward.clear()
        await self._populate_left()
        bl = self.query_one("#batch-left", ListView)
        pl = self.query_one("#proj-left", ListView)
        if self.buckets:
            bl.index = 0
            await self._load_bucket(self.buckets[0][1])
        if self.ordered:
            pl.index = 0
            await self._load_project(self.ordered[0].key)
        self._render_stats()
        self.notify(f"已刷新 · {len(self.issues)} issue")


def run(_args=None, *, roots=None):
    """Launch the interactive browser. Returns a process exit code.

    roots: optional list of (tier_name, path) tuples for multi-tier aggregation.
    When None, loads from the single current DOCKET_ROOT."""
    PMUI(roots=roots).run()
    return 0


def _selftest():  # a linear sequence of pilot-driven UI assertions  # noqa: C901, PLR0915
    """Pilot-driven assertions against the live repo (read-only) + a screenshot.
    Exits non-zero (via AssertionError) on any behavioural regression."""
    import asyncio

    async def go():  # a linear sequence of pilot-driven UI assertions  # noqa: PLR0915
        app = PMUI()
        async with app.run_test(size=(124, 40)) as pilot:
            await pilot.pause()

            tabs = app.query_one(TabbedContent)
            assert tabs.active == "tab-batch", (
                f"default tab {tabs.active!r} != tab-batch"
            )
            bl = app.query_one("#batch-left", ListView)
            assert len(bl.children) == N_BUCKETS, (
                f"批次 left has {len(bl.children)} buckets, want {N_BUCKETS}"
            )

            # 本批 = bucket index 1; select it and assert its issues populate.
            cur_count = len(app.buckets[1][1])
            bl.index = 1
            await pilot.pause()
            mid = app.query_one("#batch-issues", ListView)
            n_issues = sum(1 for c in mid.children if isinstance(c, IssueItem))
            if cur_count > 0:
                assert n_issues == cur_count, (
                    f"本批 has {n_issues} issue rows, want {cur_count}"
                )

            # switch to 项目 tab via the '2' binding; assert project list non-empty.
            await pilot.press("2")
            await pilot.pause()
            assert tabs.active == "tab-proj", f"after '2' active={tabs.active!r}"
            pl = app.query_one("#proj-left", ListView)
            assert len(pl.children) > 0, "project list empty"

            # 高亮一个 project(优选含 ## 现状 段的),详情面板应渲它自己的 body 且现状领先,
            # relations/comments 清空、current_issue_id 复位 None(浏览项目不对应某 issue)。
            md = app.query_one("#proj-md", Markdown)
            proj_idx = next(
                (
                    i
                    for i, c in enumerate(pl.children)
                    if isinstance(c, ProjItem) and "## 现状" in (c.project.body or "")
                ),
                None,
            )
            if proj_idx is None:  # 无现状段则退而求其次:任一有 body 的项目
                proj_idx = next(
                    (
                        i
                        for i, c in enumerate(pl.children)
                        if isinstance(c, ProjItem) and (c.project.body or "").strip()
                    ),
                    None,
                )
            if proj_idx is not None:
                proj_item = pl.children[proj_idx]
                pl.index = proj_idx
                await pilot.pause()
                pbody = md._markdown
                expected = _project_body_for_panel(proj_item.project.body)
                assert pbody == expected, (
                    "proj-md != reordered project body on highlight"
                )
                assert app.current_issue_id is None, (
                    "current_issue_id not None while browsing a project"
                )
                assert len(app.query_one("#proj-relations", ListView).children) == 0, (
                    "proj-relations not cleared on project highlight"
                )
                if "## 现状" in (proj_item.project.body or ""):
                    import re

                    sp = expected.find("## 现状")
                    nonstatus = next(
                        (
                            m.start()
                            for m in re.finditer(r"(?m)^(# |## (?!现状))", expected)
                        ),
                        None,
                    )
                    if nonstatus is not None:
                        assert sp < nonstatus, (
                            f"现状 (pos {sp}) not leading background heading (pos {nonstatus})"
                        )

            # highlight an issue and assert the detail markdown got real content.
            before = md._markdown
            pi = app.query_one("#proj-issues", ListView)
            target = next(
                (i for i, c in enumerate(pi.children) if isinstance(c, IssueItem)), None
            )
            assert target is not None, "no issue row in 项目 mid list"
            pi.index = target
            await pilot.pause()
            after = md._markdown
            assert after and after != before, "detail markdown not updated"
            selected = app._current_issue()
            assert selected is not None, "_current_issue None with highlight"
            meta = app._last_detail["proj"]["meta"]
            rel = app._last_detail["proj"]["relations"]
            title_line = f"{display_id(selected, app.by_key)} · {selected.title()}"
            assert title_line in meta, f"detail meta missing title: {meta[:80]!r}"
            assert title_line not in after, (
                f"detail body still contains generated title/meta: {after[:80]!r}"
            )
            assert rel, "detail relations not rendered"

            # _current_issue:此刻 项目 tab 有 issue 高亮 → 非 None。
            assert selected is not None, "_current_issue None with highlight"

            # action_copy:有高亮 issue 时不应抛(copy_to_clipboard / pbcopy 任一)。
            app.action_copy()
            await pilot.pause()

            # / 搜索:按 / 显示 input,输入 'kb' 过滤激活 tab 中栏。
            await pilot.press("/")
            await pilot.pause()
            si = app.query_one("#search", Input)
            assert si.display is True, "search input not shown after '/'"
            si.value = "kb"
            await pilot.pause()
            _, mid_id = app._active_ids()
            sm = app.query_one(f"#{mid_id}", ListView)
            rows = [c for c in sm.children if isinstance(c, IssueItem)]
            hdrs = [
                c
                for c in sm.children
                if isinstance(c, ListItem) and not isinstance(c, IssueItem)
            ]
            assert len(rows) > 0, "search 'kb' matched no issue rows"
            assert len(hdrs) == 1, f"search list has {len(hdrs)} headers, want 1"
            for c in rows:  # 每个结果都须真匹配 query
                blob = f"{display_id(c.issue, app.by_key)} {c.issue.title()}".lower()
                assert "kb" in blob, f"non-matching row in search: {c.issue.id()}"
            n_search = len(rows)

            app.save_screenshot(_selftest_screenshot_path("docket_search.svg"))

            # Esc 退出搜索 → input 隐藏 + 中栏恢复正常视图(非搜索头)。
            await pilot.press("escape")
            await pilot.pause()
            assert si.display is False, "search input still shown after escape"

            # action_open with _current_issue() None → notify + 不抛(切到无高亮的空中栏)。
            await app._populate_left()  # 复位无 issue 高亮态
            empty_lv = app.query_one(f"#{mid_id}", ListView)
            await empty_lv.clear()
            app.current_issue_id = None
            assert app._current_issue() is None, "_current_issue should be None now"
            app.action_open()  # 应只 notify,不抛
            await pilot.pause()

            # action_reload:不抛 + 两个左栏仍非空。
            await app.action_reload()
            await pilot.pause()
            assert len(app.query_one("#batch-left", ListView).children) > 0, (
                "batch-left empty after reload"
            )
            assert len(app.query_one("#proj-left", ListView).children) > 0, (
                "proj-left empty after reload"
            )

            app.save_screenshot(_selftest_screenshot_path("docket_tabs.svg"))

            # ① 层级:找一个有子任务的 issue,渲染其详情应含「子任务」;
            #    再渲染其某个子任务,应含「↑ 父」。
            parent_id = next((pid for pid, kids in app.child_idx.items() if kids), None)
            assert parent_id is not None, "no parent with children in repo"
            parent = app.by_id.get(parent_id)
            assert parent is not None, f"parent {parent_id} not in by_id"
            await app._show_detail(parent, "proj")
            await pilot.pause()
            p_rel = app._last_detail["proj"]["relations"]
            assert "子任务" in p_rel, f"parent detail missing 子任务: {p_rel[:80]!r}"
            child = app.child_idx[parent_id][0]
            rel_lv = app.query_one("#proj-relations", ListView)
            rel_idx = next(
                (
                    idx
                    for idx, c in enumerate(rel_lv.children)
                    if isinstance(c, RelationItem) and c.issue.id() == child.id()
                ),
                None,
            )
            assert rel_idx is not None, "child relation item not rendered"
            rel_lv.index = rel_idx
            rel_lv.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.current_issue_id == child.id(), (
                f"relation enter did not navigate to child {child.id()}"
            )
            assert app.nav_back and app.nav_back[-1] == parent.id(), (
                f"relation jump did not push history: {app.nav_back!r}"
            )
            c_rel = app._last_detail["proj"]["relations"]
            assert "父任务" in c_rel, f"child detail missing 父任务: {c_rel[:80]!r}"
            app.save_screenshot(
                _selftest_screenshot_path("docket_children2.svg")
            )  # 注:此处 md 为子任务详情

            await pilot.press("[")
            await pilot.pause()
            assert app.current_issue_id == parent.id(), "back did not restore parent"
            assert app.nav_forward and app.nav_forward[-1] == child.id(), (
                f"back did not push forward history: {app.nav_forward!r}"
            )
            await pilot.press("]")
            await pilot.pause()
            assert app.current_issue_id == child.id(), "forward did not restore child"

            # 重渲染父详情供截图(含子任务清单)。
            await app._show_detail(parent, "proj")
            await pilot.pause()
            app.save_screenshot(_selftest_screenshot_path("docket_children2.svg"))

            # ③a 概览 tab:按 3 激活 tab-stats,#stats-body 非空且含「状态分布」。
            await pilot.press("3")
            await pilot.pause()
            assert tabs.active == "tab-stats", f"after '3' active={tabs.active!r}"
            from rich.console import Console

            assert app._last_stats is not None, "stats body empty"
            con = Console(width=100, record=True)
            con.print(app._last_stats)
            stx = con.export_text()
            assert "进行中" in stx and "每项目" in stx, (
                f"stats missing labels: {stx[:80]!r}"
            )
            app.save_screenshot(_selftest_screenshot_path("docket_stats2.svg"))

            # ③b 「全部」桶:回批次 tab,选最后一桶(全部, all_open),批次中栏列全 open
            #     (进行中+待办+暂存),行数 = 三态计数和(无视 batch),且高亮一条能渲详情。
            await pilot.press("1")
            await pilot.pause()
            assert tabs.active == "tab-batch", f"after '1' active={tabs.active!r}"
            bl2 = app.query_one("#batch-left", ListView)
            all_idx = len(app.buckets) - 1
            assert app.buckets[all_idx][0] == "全部", (
                f"last bucket is {app.buckets[all_idx][0]!r}, want 全部"
            )
            assert app.buckets[all_idx][2] is True, "末桶 all_open flag 未置 True"
            bl2.index = all_idx
            await pilot.pause()
            mid2 = app.query_one("#batch-issues", ListView)
            open_rows = [c for c in mid2.children if isinstance(c, IssueItem)]
            n_open_expected = sum(
                1
                for i in app.issues
                if i.state_type() in ("started", "unstarted", "backlog")
            )
            assert len(open_rows) == n_open_expected, (
                f"全部 桶 has {len(open_rows)} rows, want {n_open_expected}"
            )
            otarget = next(
                (
                    idx
                    for idx, c in enumerate(mid2.children)
                    if isinstance(c, IssueItem)
                ),
                None,
            )
            if otarget is not None:
                mid2.index = otarget
                await pilot.pause()
                assert app._last_detail.get("batch", {}).get("meta"), (
                    "全部 桶 issue highlight did not render detail"
                )

            return cur_count, len(pl.children), n_search

    cur_count, n_proj, n_search = asyncio.run(go())

    # ② 主题持久化:换主题后 prefs json 含 theme 键 == app.theme。
    async def theme_go():
        app = PMUI()
        async with app.run_test(size=(124, 40)) as pilot:
            await pilot.pause()
            app.action_cycle_theme()
            await pilot.pause()
            with Path(_prefs_path()).open(encoding="utf-8") as f:
                d = json.load(f)
            assert "theme" in d, f"prefs missing theme key: {d!r}"
            assert d["theme"] == app.theme, (
                f"persisted theme {d['theme']!r} != app.theme {app.theme!r}"
            )
            return d["theme"]

    # 存/还原真实 prefs:换主题测试会写盘,别篡改用户配置目录里的 ui.json。
    _pp = _prefs_path()
    _saved = Path(_pp).open(encoding="utf-8").read() if Path(_pp).exists() else None  # noqa: SIM115 — intentional: file closed immediately after .read(), lifetime must not extend past the conditional
    try:
        persisted_theme = asyncio.run(theme_go())
    finally:
        if _saved is not None:
            with Path(_pp).open("w", encoding="utf-8") as f:
                f.write(_saved)
        elif Path(_pp).exists():
            Path(_pp).unlink()

    print(
        f"本批 {cur_count} · projects {n_proj} · 搜索kb {n_search} · theme {persisted_theme}"
    )
    print(
        "docket tabs selftest ok -> 系统临时目录下的 "
        "docket_tabs.svg / docket_search.svg / docket_children2.svg / docket_stats2.svg"
    )


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        run()
