"""CLI wiring for both entry points — `docket` (explicit, agent/script-facing)
and `pm` (ergonomic human shell) — over the shared core. Port of core.go's
Main/MainHuman/runWrapped, with Typer providing per-subcommand `--help` for free
(observation F1: the Go version only handled top-level help, so `docket set
--help` was parsed as "load issue named --help" — its biggest error source).

The telemetry + stdout/stderr capture wrapper lives here (run_wrapped), outside
Typer: we redirect sys.stdout/sys.stderr to sampling tees, run the command with
Click's standalone_mode=False so exceptions surface to us, then record one
telemetry line and exit.
"""

import contextlib
import os
import sys
import time
from pathlib import Path

import typer
from orrery_heartbeat import check_update
from typer import _click as _typer_click  # typer 0.26 vendors click here
from typer.core import TyperGroup

from . import __version__, render, telemetry
from . import commands as C
from . import projects as P
from .errors import DocketError, ExitSignal
from .gitops import cmd_history, cmd_sync
from .issue import cn_now, find_repo_root, id_num, normalize_id
from .telemetry import STDERR_CAP, STDOUT_CAP, Tee

_click_exc = _typer_click.exceptions


def _command_suggestion(
    _typo: str, commands: list[str], rest: list[str], prog: str
) -> str | None:
    """Hint for the 'noun-before-verb' mistake (e.g. `docket issue show <id>`):
    the typed group token is unknown, but a *later* token is a real command — so
    the right verb is present and the call was just over-structured. Suggest
    dropping the spurious token. Lexically-close typos (`shwo`→`show`) are already
    handled upstream by TyperGroup's own get_close_matches, so we only add this
    when that produced nothing."""
    verb = next((a for a in rest if a in commands), None)
    if verb is None:
        return None
    fixed = " ".join([prog, *rest])
    return f"Did you mean: '{verb}'? (try: {fixed})"


class _SuggestGroup(TyperGroup):
    """TyperGroup that keeps its built-in close-match suggestion and adds the
    noun-before-verb hint when no close match was found (the dominant
    `docket issue show <id>` error)."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except _click_exc.UsageError as e:
            if args and "Did you mean" not in (e.message or ""):
                prog = ctx.find_root().info_name or "docket"
                hint = _command_suggestion(args[0], list(self.commands), args[1:], prog)
                if hint:
                    e.message = f"{(e.message or '').rstrip('.')}. {hint}"
            raise


app = typer.Typer(
    cls=_SuggestGroup,
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode=None,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "docket — local-first project management CLI.\n\n"
        "ids accept 286 / DEMO-286 / demo-286 / any project prefix like TEAM-286 "
        "(the number is the canonical anchor; the prefix is a display label). "
        "batches are positive integers (rolling tranches; see `docket batch` / `docket roll`).\n\n"
        "human shell: `pm` (= 交互 TUI 浏览;`pm ov` 出文本概览) · `pm <key>` (drill a project) · "
        "`pm <id>` (print the issue's file path).\n\n"
        "repo root: $DOCKET_ROOT > cwd's .git ancestor."
    ),
)


def _load_tiers() -> dict[str, str]:
    p = Path.home() / ".config/docket/tiers.toml"
    if not p.exists():
        return {}
    import tomllib

    with p.open("rb") as f:
        data = tomllib.load(f)
    return {k: str(Path(v).expanduser()) for k, v in data.get("tiers", {}).items()}


# ---- read verbs ----


@app.command("list")
def _list(
    status: str = typer.Option("", "--status"),
    project: str = typer.Option("", "--project"),
    state_type: str = typer.Option("", "--state-type"),
    batch: str = typer.Option("", "--batch"),
    milestone: str = typer.Option("", "--milestone"),
    triage: bool = typer.Option(
        False, "--triage", help="只列 triage 待审项(含已过 TTL 的;审计/查看入口)"
    ),
):
    """list issues (table)"""
    C.cmd_list(status, state_type, project, batch, milestone, triage)


@app.command("batch")
def _batch(
    n: str = typer.Argument(
        None, help="batch number to list; omit for 本批/下批/后续 view"
    ),
):
    """show the rolling-batch view (本批/下批/后续), or list one batch"""
    C.cmd_batch(n)


@app.command("roll")
def _roll(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="skip the 本批-not-empty confirm"
    ),
):
    """roll: 下批整批提为本批(本批未清空要确认)"""
    C.cmd_roll(yes)


@app.command("active")
def _active(all: bool = typer.Option(False, "--all", help="incl. done/canceled")):
    """started + unstarted, by priority (⛔ = waiting on an open blocker)"""
    C.cmd_active(all)


@app.command("ready")
def _ready():
    """Todo issues with no open blocker — what can start now (candidate set)"""
    C.cmd_ready()


@app.command("show")
def _show(
    id: str,
    no_comments: bool = typer.Option(False, "--no-comments"),
):
    """key frontmatter + full body (+ comments by default)"""
    C.cmd_show(id, no_comments)


@app.command("path")
def _path(id: str):
    """print the issue's file path (open $(docket path 320))"""
    C.cmd_path(id)


@app.command("get")
def _get(
    id: str,
    field: str = typer.Argument(
        ...,
        help="frontmatter field name (see `docket show <id>` for an issue's "
        "fields), or `body` for the issue body",
    ),
):
    """print one frontmatter field's value (or `get <id> body`)"""
    C.cmd_get(id, field)


@app.command("search")
def _search(kw: list[str] = typer.Argument(None)):
    """grep issues + comments (title/body/comment), case-insensitive"""
    C.cmd_search(" ".join(kw or []))


@app.command("tree")
def _tree(id: str):
    """show an issue + its subtask tree (by parent)"""
    P.cmd_tree(id)


# `projects` is a sub-app so registration is a real subcommand (`projects new
# <key>`), not a magic-string dispatch: the list view is the no-subcommand
# callback, and `project <key>` (singular) stays a separate drill command.
# Creation lives under the plural `projects` because a single command can't host
# both a bare positional <key> and a `new` subcommand (the arg eats the
# subcommand name) — `projects` has no positional, so `new` is unambiguous.
projects_app = typer.Typer(cls=_SuggestGroup)
app.add_typer(projects_app, name="projects")


@projects_app.callback(invoke_without_command=True)
def _projects(
    ctx: typer.Context,
    all: bool = typer.Option(False, "--all", help="incl. archived"),
):
    """project-layer overview (progress per project); `projects new <key>` registers one"""
    if ctx.invoked_subcommand is None:
        P.cmd_projects(all)


@projects_app.command("new")
def _projects_new(
    key: str = typer.Argument(..., help="project key to register"),
    title: str = typer.Option("", "--title", help="display title (default: key)"),
    prefix: str = typer.Option(
        "", "--prefix", help="id prefix, e.g. TEAM (default: KEY upper)"
    ),
    domain: str = typer.Option("pm", "--domain", help="domain field"),
    lane: str = typer.Option(
        "non-work", "--lane", help="project lane: work or non-work"
    ),
    rank: str = typer.Option(
        "", "--rank", help="project sort rank within equal activity"
    ),
    status: str = typer.Option("active", "--status", help="status field"),
):
    """register a project: write projects/<key>.md (frontmatter + heading body)"""
    C.cmd_project_new(
        key,
        title=title,
        prefix=prefix,
        domain=domain,
        status=status,
        lane=lane,
        rank=rank,
    )


@app.command("project")
def _project(key: str):
    """drill one project: plan + its issues grouped by status"""
    P.cmd_project(key)


@app.command("ui")
def _ui():
    """interactive read-only browser (project → issue → body+comments)"""
    # Normal `docket ui` / `pm ui` is intercepted in the entry points BEFORE the
    # telemetry tee (a TUI must own the real terminal); this registration is for
    # `--help` discoverability and direct _cli calls.
    from .ui import run

    run()


@app.command("history")
def _history(id: str):
    """commit history of an issue (+ its comments), newest first"""
    cmd_history(id)


@app.command("stats")
def _stats():
    """telemetry summary: per-verb count/p50/p95/max + error rate.

    Ledger:
    $DOCKET_TELEMETRY_DB, else the user data dir under docket/telemetry.db.
    Disable recording with DOCKET_TELEMETRY_OFF=1 or DO_NOT_TRACK=1; reset by
    deleting the ledger file."""
    print(telemetry.stats())


@app.command("sync")
def _sync():
    """sweep issues/comments/projects edited outside docket into history (one commit each)"""
    cmd_sync()


# ---- write verbs ----


@app.command("new")
def _new(
    title: str = typer.Argument(
        ...,
        help="issue 标题:[域] 动作 结果(如 `groom – 超长 comment 未被 surface`)",
    ),
    project: str = typer.Option("", "--project"),
    priority: str = typer.Option("No priority", "--priority"),
    batch: str = typer.Option(
        None, "--batch", help="rolling batch (positive int); default: unbatched"
    ),
    milestone: str = typer.Option("", "--milestone"),
    wake: str = typer.Option(
        None,
        "--wake",
        help="snooze until YYYY-MM-DD: hide from active/overview while future",
    ),
    parent: str = typer.Option("", "--parent", help="parent issue id (F3)"),
    body: str = typer.Option("", "--body", help='issue body; "-" reads stdin (F3)'),
    status: str = typer.Option(
        None, "--status", help="initial state, e.g. backlog (default: Todo)"
    ),
    blocked_by: list[str] = typer.Option(
        None,
        "--blocked-by",
        help="dependency edge: issue id this waits on (repeatable)",
    ),
    new_project: bool = typer.Option(
        False,
        "--new-project",
        help="if --project isn't registered, create projects/<key>.md on the fly",
    ),
    directed: bool = typer.Option(
        False,
        "--directed",
        help="principal 点名建:直进 Todo,不进 triage 入口闸 (ADR-008)",
    ),
    triage: bool = typer.Option(
        None,
        "--triage/--no-triage",
        help="显式覆盖 triage 入口闸 (默认:agent 上下文进闸, 裸 human 直进)",
    ),
    actor: str = typer.Option(
        None,
        "--actor",
        help="署名 + 定 actor(决定 triage 闸默认;非 human 即进闸)",
    ),
    type: str = typer.Option(
        "task",
        "--type",
        help="省略 --body 时的 body 骨架:task=SCQA 六轴(默认)/ bug=复现核心",
    ),
):
    """create issue (id = max+1)"""
    C.cmd_new(
        title,
        project,
        priority,
        batch,
        milestone,
        parent,
        body,
        status=status,
        blocked_by=blocked_by,
        new_project=new_project,
        wake=wake,
        directed=directed,
        triage=triage,
        actor=actor,
        type_=type,
    )


@app.command("set")
def _set(
    id: str,
    priority: str = typer.Option(None, "--priority"),
    project: str = typer.Option(None, "--project"),
    batch: str = typer.Option(None, "--batch"),
    milestone: str = typer.Option(None, "--milestone"),
    title: str = typer.Option(None, "--title"),
    parent: str = typer.Option(None, "--parent"),
    status: str = typer.Option(
        None, "--status", help="workflow state, e.g. backlog/started/Done"
    ),
    blocked_by: list[str] = typer.Option(
        None,
        "--blocked-by",
        help="add a dependency edge: waits on this id (repeatable)",
    ),
    unblock: list[str] = typer.Option(
        None, "--unblock", help="remove a dependency edge (repeatable)"
    ),
    wake: str = typer.Option(
        None,
        "--wake",
        help="snooze until YYYY-MM-DD: hide from active/overview while future",
    ),
    unwake: bool = typer.Option(False, "--unwake", help="clear the snooze (wake)"),
):
    """edit fields incl. --status (bumps updated)"""
    C.cmd_set(
        id,
        priority=priority,
        project=project,
        batch=batch,
        milestone=milestone,
        title=title,
        parent=parent,
        status=status,
        blocked_by=blocked_by,
        unblock=unblock,
        wake=wake,
        unwake=unwake,
    )


@app.command("start")
def _start(id: str):
    """-> In Progress / started"""
    C.cmd_start(id)


@app.command("finish")
def _finish(id: str):
    """-> Done / completed"""
    C.cmd_finish(id)


@app.command("status")
def _status(id: str, state: str):
    """set any state (state_type or display status)"""
    C.cmd_status(id, state)


# ---- triage entry gate (ADR-008) ----


@app.command("triage")
def _triage(
    gc: bool = typer.Option(
        False, "--gc", help="物化过期待审项为 canceled (TTL 自愈整洁)"
    ),
):
    """list the triage inbox (active proposals awaiting accept/decline)"""
    C.cmd_triage(gc=gc)


@app.command("accept")
def _accept(
    id: str,
    backlog: bool = typer.Option(
        False, "--backlog", help="accept 到 Backlog 而非 Todo"
    ),
):
    """accept a triage proposal → Todo (or --backlog → Backlog)"""
    C.cmd_accept(id, backlog=backlog)


@app.command("decline")
def _decline(id: str, reason: list[str] = typer.Argument(None)):
    """decline a triage proposal → Canceled (+ 留痕 comment)"""
    C.cmd_decline(id, " ".join(reason or []))


@app.command("comment")
def _comment(
    id: str,
    text: list[str] = typer.Argument(None),
    actor: str | None = typer.Option(
        None,
        "--actor",
        "--source",
        "--author",
        help="comment source label; --author is a legacy alias",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help="comment session id; defaults to DOCKET/Codex/Claude env when present",
    ),
    amend: bool = typer.Option(
        False, "--amend", help="replace the last comment with text"
    ),
    delete_last: bool = typer.Option(
        False, "--delete-last", help="drop the last comment"
    ),
):
    """append / --amend / --delete-last a comment in comments/ISSUE-<n>.md"""
    C.cmd_comment(
        id,
        actor,
        " ".join(text or []),
        amend=amend,
        delete_last=delete_last,
        session=session,
    )


# ---- overview / validate / roundtrip (aliases + hidden) ----


def _overview(
    no_projects: bool = typer.Option(
        False, "--no-projects", help="省掉「项目」进度条表, 只出 在飞+本批"
    ),
):
    """human index: 在飞 + 每项目进度 + 指向下一条 issue"""
    P.cmd_overview(show_projects=not no_projects)


app.command("overview")(_overview)
app.command("ov")(_overview)


def _validate(
    strict: bool = typer.Option(
        False,
        "--strict",
        help="also fail on work-surface drift such as in-scope issues without project",
    ),
):
    """validate all issues (exit 1 on problems)"""
    C.cmd_validate(strict=strict)


app.command("validate")(_validate)
app.command("lint")(_validate)


@app.command("health")
def _health(
    project: str = typer.Option("", "--project", help="only this project (substring)"),
    json: bool = typer.Option(False, "--json", help="structured signal envelope"),
):
    """agent-facing advisory work-health signals"""
    C.cmd_health(project=project, as_json=json)


@app.command("groom")
def _groom(
    project: str = typer.Option("", "--project", help="only this project (substring)"),
    json: bool = typer.Option(False, "--json", help="structured records for fan-out"),
    today: str = typer.Option(
        "", "--today", help="pin today=YYYY-MM-DD (default: now; for tests/reports)"
    ),
):
    """staleness table of every non-done issue (periodic triage;零 LLM)"""
    C.cmd_groom(project=project, as_json=json, today_str=today)


@app.command("orphans")
def _orphans(
    repo: str = typer.Option("", "--repo", help="git repo to scan (default: cwd)"),
    limit: int = typer.Option(200, "--limit", help="scan the last N commits"),
    json: bool = typer.Option(False, "--json", help="structured records for fan-out"),
):
    """『提交了没关单』检测:最近 commit 引用的 open issue(read-only, bd doctor 类比)"""
    C.cmd_orphans(repo=repo, limit=limit, as_json=json)


@app.command("roundtrip", hidden=True)
def _roundtrip(id: str):
    """parse an issue and write it back verbatim (fidelity check)"""
    C.cmd_roundtrip(id)


# ---- the click command (built once, after all registrations) ----

_cli = typer.main.get_command(app)


# ---- telemetry + capture wrapper ----

_HELP_ARGS = {"help", "-h", "--help", ""}


def _safe_root() -> str:
    try:
        return find_repo_root()
    except Exception:
        return ""


def run_wrapped(invoker: str, verb: str, rest: list) -> None:
    """Run one verb behind the color-init + stdout/stderr-capture + telemetry
    wrapper, then exit. Mirrors core.go's runWrapped. Never called concurrently."""
    start = time.monotonic()

    # Decide color from the REAL terminal BEFORE redirecting stdout to the tee.
    try:
        is_tty = sys.stdout.isatty()
    except Exception:
        is_tty = False
    render.init_color(is_tty)

    real_out, real_err = sys.stdout, sys.stderr
    out_tee = Tee(real_out, STDOUT_CAP)
    err_tee = Tee(real_err, STDERR_CAP)
    sys.stdout, sys.stderr = out_tee, err_tee

    exit_code = 0
    err_msg = ""
    app_args = ["--help"] if verb in _HELP_ARGS else [verb, *rest]
    try:
        # The per-write tree scan moved to `docket sync`: a write now
        # only self-commits its own file, so concurrent writers don't fight.
        _cli(args=app_args, standalone_mode=False, prog_name=invoker)
    except ExitSignal as e:
        exit_code = e.code  # intentional non-zero, NOT a tool error (err stays "")
    except DocketError as e:
        print("error:", e.message, file=sys.stderr)
        err_msg = e.message
        exit_code = 1
    except _click_exc.Abort:
        print("Aborted!", file=sys.stderr)
        exit_code = 1
    except _click_exc.Exit as e:
        exit_code = int(e.exit_code)
    except _click_exc.ClickException as e:
        e.show()  # prints "Usage: ... Error: ..." to stderr
        err_msg = e.format_message()
        exit_code = e.exit_code if e.exit_code else 2
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception as e:
        # Go returned every failure as an error -> clean `error:` line + telemetry.
        # Without this, an OSError/UnicodeError/etc. would escape as a traceback
        # and skip telemetry. (critic BUG 2)
        print("error:", e, file=sys.stderr)
        err_msg = str(e)
        exit_code = 1
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    # consume the template's SQLite telemetry (record), default user-data-dir db.
    # docket-specific context (invoker docket/pm + active data root) rides in meta.
    telemetry.record(
        {
            "ts": cn_now().isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "command_path": [verb],
            "args": list(rest),
            "cwd": str(Path.cwd()),
            "exit_code": exit_code,
            "duration_ms": int((time.monotonic() - start) * 1000),
            "out_bytes": out_tee.total,
            "stdout": out_tee.sample,
            "stderr": err_tee.sample,
            "err": err_msg,
            "version": __version__,
            "is_tty": is_tty,
            "is_ci": bool(os.environ.get("CI")),
            "meta": {"invoker": invoker, "root": _safe_root()},
        }
    )
    sys.exit(exit_code)


# ---- entry points ----

_KNOWN_VERBS = {
    "list",
    "overview",
    "ov",
    "projects",
    "project",
    "tree",
    "active",
    "ready",
    "batch",
    "roll",
    "show",
    "new",
    "set",
    "search",
    "comment",
    "start",
    "finish",
    "status",
    "triage",
    "accept",
    "decline",
    "history",
    "stats",
    "sync",
    "validate",
    "lint",
    "health",
    "groom",
    "orphans",
    "roundtrip",
    "path",
    "get",
    "ui",
    "-h",
    "--help",
    "help",
}


def _maybe_launch_ui(argv: list) -> None:
    """`docket ui` / `pm ui` launches the Textual browser DIRECTLY — before
    run_wrapped's stdout/stderr tee, because a full-screen TUI must own the real
    terminal (the tee would capture its escape stream). `ui --help`/`-h` falls
    through to Typer so the normal help still works."""
    if argv and argv[0] == "ui" and not any(a in ("-h", "--help") for a in argv[1:]):
        from .ui import run as _ui_run

        tiers = _load_tiers()
        roots = list(tiers.items()) if tiers else None
        raise SystemExit(_ui_run(argv[1:], roots=roots))


def _known_verb(s: str) -> bool:
    return s in _KNOWN_VERBS


def _is_project_key(s: str) -> bool:
    try:
        by_key, _ = P.load_projects()
    except Exception:
        return False
    else:
        return s in by_key


def _is_id(s: str) -> bool:
    _, ok = id_num(normalize_id(s))
    return ok


def _relax_std_encoding() -> None:
    """Match Go's byte-lossless stdout: surrogateescape lets us print raw non-UTF-8
    bytes (round-tripped via surrogateescape from a stray-byte file) instead of
    crashing on encode. No-op if the stream lacks reconfigure."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="surrogateescape")


def _consume_tier(argv: list[str]) -> list[str]:
    """Extract --tier <name> from argv, set DOCKET_ROOT, return remaining argv."""
    if "--tier" not in argv:
        return argv
    idx = argv.index("--tier")
    if idx + 1 >= len(argv):
        print("error: --tier requires a value", file=sys.stderr)
        sys.exit(2)
    tier_name = argv[idx + 1]
    tiers = _load_tiers()
    if tier_name not in tiers:
        avail = (
            ", ".join(tiers)
            if tiers
            else "(none configured in $XDG_CONFIG_HOME/docket/tiers.toml)"
        )
        print(f"error: unknown tier '{tier_name}'. Available: {avail}", file=sys.stderr)
        sys.exit(2)
    os.environ["DOCKET_ROOT"] = tiers[tier_name]
    return argv[:idx] + argv[idx + 2 :]


def main_docket() -> None:
    """`docket` entry point: explicit, agent/script-facing. argv -> verb, no magic."""
    check_update("docket", "the-orrery/docket")
    _relax_std_encoding()
    argv = _consume_tier(sys.argv[1:])
    _maybe_launch_ui(argv)
    if not argv:
        print(
            "usage: docket <command> [...]  —  run `docket --help` for the full list",
            file=sys.stderr,
        )
        sys.exit(2)
    run_wrapped("docket", argv[0], argv[1:])


def main_pm() -> None:
    """`pm` entry point: ergonomic human shell. Bare `pm` = overview; `pm <key>`
    = drill that project; `pm <id>` = print the issue's file path; anything else
    passes through to the normal verbs."""
    check_update("docket", "the-orrery/docket")
    _relax_std_encoding()
    argv = _consume_tier(sys.argv[1:])
    _maybe_launch_ui(argv)
    if not argv:
        from .ui import run as _ui_run

        tiers = _load_tiers()
        roots = list(tiers.items()) if tiers else None
        raise SystemExit(_ui_run([], roots=roots))
    if _known_verb(argv[0]):
        verb, rest = argv[0], argv[1:]
    elif _is_project_key(argv[0]):
        verb, rest = "project", argv[:1]
    elif _is_id(argv[0]):
        verb, rest = "path", argv[:1]
    else:
        verb, rest = argv[0], argv[1:]  # passthrough; the app reports unknown
    run_wrapped("pm", verb, rest)
