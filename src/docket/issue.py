"""Issue data model + frontmatter round-trip + loaders. Port of issue.go.

Round-trip fidelity is the contract `docket roundtrip` guards: parsing then
re-rendering an untouched issue must reproduce the file byte for byte. We keep
each frontmatter field's value as VERBATIM text (everything after "key: ") and
only re-render a value when a caller explicitly changes it.
"""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path
from typing import NoReturn

from .errors import DocketError

# ---- scalar quoting (matches the files' simple double-quote style) ----


def unquote_scalar(s: str) -> str:
    s = s.strip()
    if len(s) > 1 and s[0] == '"' and s[-1] == '"':
        inner = s[1:-1]
        # these files use only simple double-quoting; unescape the two YAML
        # double-quote escapes that can appear (same order as the Go original).
        inner = inner.replace('\\"', '"')
        return inner.replace("\\\\", "\\")
    return s


def quote_scalar(s: str) -> str:
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    return '"' + s + '"'


def parse_flow_list(s: str) -> list[str]:
    """Parse docket's simple flow-list fields (``[A, B]``) into strings."""
    raw = s.strip()
    if raw in {"", "[]"}:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [
        part.strip().strip('"').strip("'") for part in raw.split(",") if part.strip()
    ]


def unique_refs(refs) -> list[str]:
    """Return refs de-duplicated in first-seen order, dropping blanks."""
    seen = set()
    out = []
    for ref in refs:
        value = str(ref or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def format_flow_list(refs) -> str:
    return "[" + ", ".join(unique_refs(refs)) + "]"


_FM_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:[ \t]+(.*))?$")

#: Triage TTL (ADR-008): an un-accepted `triage: true` proposal self-heals this
#: many days after `created` — read-time only, never written (mirrors wake's
#: is_snoozed read-time algorithm), so it drops out of the inbox/nag/work surfaces
#: without a daemon. Fixed constant (承 adr-007 反 knob); changing it needs a new ADR.
TRIAGE_TTL_DAYS = 14


def parse_bool(s: str):
    """Parse a frontmatter boolean ("true"/"false", case-insensitive) -> (val, ok).
    ok=False for any other non-empty value, so validate can flag a bad triage."""
    t = s.strip().lower()
    if t == "true":
        return True, True
    if t == "false":
        return False, True
    return False, False


class Issue:
    """Parsed frontmatter (order-preserving) + verbatim body."""

    __slots__ = ("body", "fields", "path")

    def __init__(self, path: str = "", fields=None, body: str = ""):
        self.path = path
        # list of [key, val]; val = verbatim text after "key: " (no newline).
        self.fields = fields if fields is not None else []
        self.body = body

    # -- raw field access --

    def get(self, key: str):
        for k, v in self.fields:
            if k == key:
                return v, True
        return "", False

    def set(self, key: str, val: str) -> None:
        """Update an existing field in place (preserving position), else insert
        before "labels" if present, else append."""
        for f in self.fields:
            if f[0] == key:
                f[1] = val
                return
        for i, f in enumerate(self.fields):
            if f[0] == "labels":
                self.fields.insert(i, [key, val])
                return
        self.fields.append([key, val])

    def set_after(self, key: str, val: str, anchor: str) -> None:
        """Update in place, or — if absent — insert immediately after `anchor`
        (so batch/milestone land between project and parent). Falls back to set()
        if anchor is missing."""
        for f in self.fields:
            if f[0] == key:
                f[1] = val
                return
        for i, f in enumerate(self.fields):
            if f[0] == anchor:
                self.fields.insert(i + 1, [key, val])
                return
        self.set(key, val)

    def remove(self, key: str) -> bool:
        """Drop a field if present; return whether it was there."""
        for i, f in enumerate(self.fields):
            if f[0] == key:
                del self.fields[i]
                return True
        return False

    # -- render / write --

    def render(self) -> str:
        parts = ["---\n"]
        for k, v in self.fields:
            if v == "":
                parts.append(k + ":\n")
            else:
                parts.append(k + ": " + v + "\n")
        parts.append("---\n")
        parts.append(self.body)
        return "".join(parts)

    def write(self) -> None:
        from .fsops import atomic_write_file

        atomic_write_file(self.path, self.render(), 0o644)

    # -- typed accessors (logical values) --

    def id(self) -> str:
        v, _ = self.get("id")
        return v.strip()

    def uid(self) -> str:
        v, _ = self.get("uid")
        return v.strip()

    def aliases(self) -> list[str]:
        v, ok = self.get("aliases")
        if not ok:
            return []
        return parse_flow_list(v)

    def set_aliases(self, refs) -> None:
        cleaned = unique_refs(refs)
        if not cleaned:
            self.remove("aliases")
            return
        self.set_after("aliases", format_flow_list(cleaned), "uid")

    def project_iid(self):
        v, ok = self.get("project_iid")
        if not ok:
            return None
        try:
            n = int(v.strip())
        except ValueError:
            return None
        return n if n > 0 else None

    def title(self) -> str:
        v, _ = self.get("title")
        return unquote_scalar(v)

    def status(self) -> str:
        v, _ = self.get("status")
        return v.strip()

    def state_type(self) -> str:
        v, _ = self.get("state_type")
        return v.strip()

    def priority(self) -> str:
        v, _ = self.get("priority")
        return v.strip()

    def project(self) -> str:
        v, _ = self.get("project")
        return unquote_scalar(v)

    def parent(self) -> str:
        v, _ = self.get("parent")
        return v.strip()

    def batch(self):
        """Rolling-batch ordinal (positive int) or None if unset/blank/non-int."""
        v, ok = self.get("batch")
        if not ok:
            return None
        try:
            return int(v.strip())
        except ValueError:
            return None

    def milestone(self) -> str:
        v, _ = self.get("milestone")
        return unquote_scalar(v)

    def wake(self) -> str:
        """Snooze-until date (YYYY-MM-DD) or "" when unset. An open issue whose
        wake is in the future is hidden from active/overview (it's blocked on
        something external, can't be pushed now); on/before today it surfaces
        again as "睡醒待看"."""
        v, _ = self.get("wake")
        return v.strip()

    def is_snoozed(self) -> bool:
        """Open (not completed/canceled) AND wake is set to a future date: this
        issue is asleep — hidden from active/overview until wake ≤ today."""
        if self.state_type() in ("completed", "canceled"):
            return False
        w = self.wake()
        return w != "" and w > today()

    def is_awake_due(self) -> bool:
        """Open AND wake is set on/before today: the snooze has elapsed, so the
        issue is back and worth a look ("睡醒待看")."""
        if self.state_type() in ("completed", "canceled"):
            return False
        w = self.wake()
        return w != "" and w <= today()

    def set_wake(self, date: str) -> None:
        """Write wake (after milestone/batch/project, before parent — same
        grouping cascade as set/new use for milestone)."""
        anchor = "project"
        if self.get("milestone")[1]:
            anchor = "milestone"
        elif self.get("batch")[1]:
            anchor = "batch"
        self.set_after("wake", date, anchor)

    def triage(self) -> bool:
        """Whether this issue carries `triage: true` — the entry-gate holding pen
        (ADR-008) for agent/hook/recall-proposed issues awaiting principal review.
        The underlying state_type stays unstarted; accept揭掉字段 → normal Todo."""
        v, ok = self.get("triage")
        if not ok:
            return False
        val, vok = parse_bool(v)
        return val if vok else False

    def is_triage(self) -> bool:
        """Carrying triage:true (regardless of TTL). Work surfaces hide on THIS —
        a triage item never enters the work face before being accepted."""
        return self.triage()

    def is_triage_expired(self) -> bool:
        """triage AND created + TTL(14d) < today: the un-accepted proposal aged
        out and is read-time self-healed (drops from inbox/nag). Read only — never
        written (同 wake 的 is_snoozed 读时算法)."""
        if not self.triage():
            return False
        cv, _ = self.get("created")
        c, ok = parse_date_ymd(cv)
        if not ok:
            return False
        deadline = (c + timedelta(days=TRIAGE_TTL_DAYS)).strftime("%Y-%m-%d")
        return deadline < today()

    def is_triage_active(self) -> bool:
        """triage and not yet TTL-expired — the live review set (inbox/nag use
        this; expired items auto-drain)."""
        return self.is_triage() and not self.is_triage_expired()

    def blocked_by(self) -> list:
        """Dependency edges: ids this issue waits on, from a flow list like
        "[DEMO-1, DEMO-2]". Stored one-way; reverse edges (blocks) are computed."""
        v, ok = self.get("blocked_by")
        if not ok:
            return []
        return [normalize_id(p) for p in parse_flow_list(v)]

    def set_blocked_by(self, ids) -> None:
        """Write the blocked_by list (after parent); drop the field when empty."""
        if not ids:
            self.remove("blocked_by")
            return
        self.set_after("blocked_by", format_flow_list(ids), "parent")


def _parse_fm(is_: Issue, block: str, base: str) -> None:
    for line in block.rstrip("\n").split("\n"):
        if line.strip() == "":
            continue
        m = _FM_LINE_RE.match(line)
        if m is None:
            raise DocketError(f'{base}: unparseable frontmatter line: "{line}"')
        is_.fields.append([m.group(1), m.group(2) or ""])


def parse_issue(path: str) -> Issue:
    """Read a file and split it into ordered frontmatter fields + a verbatim body.
    The body retains its exact bytes (incl. trailing newline). surrogateescape
    keeps any non-UTF-8 byte losslessly (Go held files as raw-byte strings), so a
    stray byte round-trips instead of crashing the parser."""
    with Path(path).open(encoding="utf-8", errors="surrogateescape") as f:
        text = f.read()
    base = Path(path).name
    if not text.startswith("---\n"):
        raise DocketError(f"{base}: missing opening frontmatter delimiter")
    rest = text[len("---\n") :]
    end = rest.find("\n---\n")
    if end < 0:
        # allow closing delimiter at very end without trailing body newline
        if rest.endswith("\n---"):
            fm_block = rest[: len(rest) - len("---")]
            is_ = Issue(path=path, body="")
            _parse_fm(is_, fm_block, base)
            return is_
        raise DocketError(f"{base}: missing closing frontmatter delimiter")
    fm_block = rest[: end + 1]  # include trailing newline of last fm line
    body = rest[end + len("\n---\n") :]
    is_ = Issue(path=path, body=body)
    _parse_fm(is_, fm_block, base)
    return is_


# ---- repo root / collection helpers ----


def _has_issues(d: str) -> bool:
    return (Path(d) / "issues").is_dir()


def find_repo_root() -> str:
    """Locate the PM data repo root, in priority order:
    1. $DOCKET_ROOT (if set and the directory exists)
    2. walk up from cwd to a directory containing .git
    """
    env = os.environ.get("DOCKET_ROOT", "").strip()
    if env != "":
        if Path(env).is_dir():
            return env
        raise DocketError(f'DOCKET_ROOT="{env}" is not an existing directory')

    d = Path.cwd()
    while True:
        if (d / ".git").exists():
            return str(d)
        parent = d.parent
        if parent == d:
            raise DocketError(
                "could not locate the PM data repo root: set DOCKET_ROOT to the repo "
                "path, or run from inside a PM data repository"
            )
        d = parent


def issues_dir() -> str:
    root = find_repo_root()
    d = str(Path(root) / "issues")
    if not Path(d).is_dir():
        raise DocketError(f"issues/ dir not found at repo root {root}")
    return d


# ---- id handling ----

#: Default canonical id prefix for newly minted issues and the display fallback.
#: Keeps the build default at the neutral "ISSUE"; set $DOCKET_ID_PREFIX (sharing
#: the repo's DOCKET_ env namespace) to re-source data whose canonical ids use a
#: different prefix. Read live so the env can be flipped per process / per test.
_DEFAULT_ID_PREFIX = "ISSUE"


def id_prefix() -> str:
    """The canonical id prefix in effect ($DOCKET_ID_PREFIX, else "ISSUE")."""
    return os.environ.get("DOCKET_ID_PREFIX", "").strip() or _DEFAULT_ID_PREFIX


_ANY_PREFIX_ID_RE = re.compile(r"^[A-Za-z]+-(\d+)$")
_BARE_NUMBER_RE = re.compile(r"^\d+$")
_UID_RE = re.compile(r"^dkt_[0-9a-f]{32}$")


def new_uid() -> str:
    """Return a machine-stable docket uid for a newly minted or migrated issue."""
    return f"dkt_{uuid.uuid4().hex}"


def is_uid(ref: str) -> bool:
    return _UID_RE.fullmatch(ref.strip()) is not None


def id_num(id_: str):
    """Return (n, True) for a canonical "<prefix>-N" id, else (0, False)."""
    m = _ANY_PREFIX_ID_RE.match(id_)
    if m is None:
        return 0, False
    if id_[: id_.index("-")] != id_prefix():
        return 0, False
    return int(m.group(1)), True


def normalize_id(s: str) -> str:
    """Legacy normalization to the storage id prefix.

    New command entry points should use ``load_by_id`` / ``resolve_issue_ref`` so
    uid, project-local display refs, and aliases are honored. This helper remains
    for legacy storage-id sorting, suggestions, and old tests.
    """
    s = s.strip()
    m = _ANY_PREFIX_ID_RE.match(s)
    if m is not None:
        return f"{id_prefix()}-{m.group(1)}"
    try:
        return f"{id_prefix()}-{int(s, 10)}"
    except ValueError:
        return s


def max_id(issues) -> int:
    mx = 0
    for is_ in issues:
        n, ok = id_num(is_.id())
        if ok and n > mx:
            mx = n
    return mx


def sort_by_priority(issues) -> None:
    """Stable sort in place by priority rank (unknown -> 99), then ascending id."""
    from .states import PRIORITY_RANK

    issues.sort(
        key=lambda is_: (PRIORITY_RANK.get(is_.priority(), 99), id_num(is_.id())[0])
    )


def load_all():
    """Read every issue file under issues/, sorted by numeric id. Lists all
    *.md rather than globbing a single prefix so re-sourced data carrying mixed
    canonical prefixes (and any non-default $DOCKET_ID_PREFIX) is never dropped."""
    d = issues_dir()
    paths = [str(p) for p in Path(d).glob("*.md")]
    issues = [parse_issue(p) for p in paths]
    issues.sort(key=lambda is_: id_num(is_.id())[0])
    return issues


class _IssueRefProject:
    __slots__ = ("prefix",)

    def __init__(self, prefix: str = ""):
        self.prefix = prefix


def _project_field(is_: Issue, key: str) -> str:
    value, _ = is_.get(key)
    return value


def _load_issue_ref_projects() -> dict[str, _IssueRefProject]:
    """Load only project keys and prefixes without importing projects.py.

    projects.py renders the project views and imports issue.py, so the issue
    resolver keeps a tiny local loader for its default path. Higher-level callers
    can still pass the full projects map from projects.load_projects().
    """
    try:
        root = find_repo_root()
    except DocketError:
        return {}
    projects: dict[str, _IssueRefProject] = {}
    for path in sorted((Path(root) / "projects").glob("*.md")):
        try:
            project_issue = parse_issue(str(path))
        except Exception:
            continue
        key = unquote_scalar(_project_field(project_issue, "key")).strip()
        prefix = unquote_scalar(_project_field(project_issue, "prefix")).strip()
        projects[key or path.stem] = _IssueRefProject(prefix)
    return projects


def _issue_display_ref(is_: Issue, projects) -> str:
    """Compute the current human ref.

    With identity v3, project_iid is the project-local human number. Older data
    without project_iid falls back to the legacy global id number so existing PM
    roots remain readable before migration.
    """
    p = projects.get(is_.project())
    if p is None or p.prefix == "":
        return is_.id()
    iid = is_.project_iid()
    if iid is not None:
        return f"{p.prefix}-{iid}"
    n, ok = id_num(is_.id())
    return f"{p.prefix}-{n}" if ok else is_.id()


def issue_refs(is_: Issue, projects=None) -> list[str]:
    """All stable refs that should resolve to this issue."""
    if projects is None:
        projects = _load_issue_ref_projects()
    refs = [is_.uid(), is_.id(), _issue_display_ref(is_, projects), *is_.aliases()]
    return unique_refs(refs)


def _known_display_prefixes(projects) -> set[str]:
    prefixes = {id_prefix()}
    for p in projects.values():
        if p.prefix:
            prefixes.add(p.prefix)
    return prefixes


def _format_candidates(issues, projects) -> str:
    labels = []
    for is_ in issues:
        display = _issue_display_ref(is_, projects)
        label = display
        if display != is_.id():
            label = f"{display} ({is_.id()})"
        if is_.uid():
            label = f"{label} uid={is_.uid()}"
        labels.append(label)
    return ", ".join(labels)


def _closest_ids(id_: str, existing: list[str]) -> list[str]:
    """Nearest existing canonical ids for a not-found lookup. The shared
    "<prefix>-" stays in the character ratio, so a fat-finger number or a
    prefix that normalized onto a non-existent number surfaces the numerically
    closest real ids first."""
    return get_close_matches(id_, existing, n=3, cutoff=0.6)


def _exact_ref_matches(raw: str, issues, projects) -> dict[str, Issue]:
    matches: dict[str, Issue] = {}
    raw_key = raw.lower()
    for is_ in issues:
        for candidate in issue_refs(is_, projects):
            if candidate.lower() == raw_key:
                matches[is_.id()] = is_
    return matches


def _bare_number_matches(raw: str, issues) -> tuple[dict[str, Issue], str]:
    if not _BARE_NUMBER_RE.fullmatch(raw):
        return {}, raw
    n = int(raw, 10)
    legacy = f"{id_prefix()}-{n}"
    matches = {
        is_.id(): is_ for is_ in issues if is_.id() == legacy or is_.project_iid() == n
    }
    return matches, legacy


def _legacy_prefix_matches(raw: str, issues, projects) -> tuple[dict[str, Issue], str]:
    m = _ANY_PREFIX_ID_RE.match(raw)
    if m is None:
        return {}, raw
    prefix = raw[: raw.index("-")]
    registered = {p.lower() for p in _known_display_prefixes(projects)}
    if prefix.lower() in registered:
        return {}, raw
    legacy = f"{id_prefix()}-{m.group(1)}"
    matches = {is_.id(): is_ for is_ in issues if is_.id() == legacy}
    return matches, legacy


def _raise_ref_not_found(raw: str, not_found_ref: str, issues, projects) -> NoReturn:
    existing_refs = []
    for is_ in issues:
        existing_refs.extend(issue_refs(is_, projects))
    normalized = normalize_id(raw)
    sugg = _closest_ids(normalized, existing_refs)
    hint = f". Did you mean: {', '.join(sugg)}?" if sugg else ""
    raise DocketError(f"issue {not_found_ref} not found{hint}")


def resolve_issue_ref(ref: str, issues=None, projects=None) -> Issue:
    """Resolve uid/display ref/alias/legacy id to one Issue.

    Resolution is intentionally fail-closed: exact refs win; legacy foreign
    prefix fallback only applies when that prefix is not a registered project
    prefix. Bare numbers remain accepted for old workflows, but they must map to
    exactly one candidate.
    """
    raw = ref.strip()
    if raw == "":
        raise DocketError("issue ref is empty")
    if issues is None:
        issues = load_all()
    if projects is None:
        projects = _load_issue_ref_projects()

    matches = _exact_ref_matches(raw, issues, projects)
    not_found_ref = raw

    if not matches:
        matches, not_found_ref = _bare_number_matches(raw, issues)
    if not matches:
        matches, not_found_ref = _legacy_prefix_matches(raw, issues, projects)

    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        raise DocketError(
            f"{raw}: ambiguous issue ref; candidates: {_format_candidates(matches.values(), projects)}"
        )

    _raise_ref_not_found(raw, not_found_ref, issues, projects)


def load_by_id(id_: str) -> Issue:
    return resolve_issue_ref(id_)


def next_project_iid(issues, project: str) -> int:
    mx = 0
    for is_ in issues:
        if is_.project() != project:
            continue
        iid = is_.project_iid()
        if iid is not None and iid > mx:
            mx = iid
    return mx + 1


# ---- time (fixed display timezone, UTC+8, not the host's) ----

_CN_ZONE = timezone(timedelta(hours=8))


def cn_now() -> datetime:
    return datetime.now(_CN_ZONE)


def today() -> str:
    return cn_now().strftime("%Y-%m-%d")


def parse_date_ymd(s):
    """Parse a YYYY-MM-DD frontmatter date -> (datetime, ok). ok=False on blank or
    an unparseable value (mirrors commands.parse_date, kept local to issue.py so
    the typed accessors don't import upward)."""
    s = s.strip()
    if s == "":
        return None, False
    try:
        return datetime.strptime(s, "%Y-%m-%d"), True
    except ValueError:
        return None, False


def parse_batch(s: str):
    """Parse a batch value -> (n, ok). ok=False for blank or non-positive-int."""
    s = s.strip()
    if s == "":
        return 0, False
    try:
        n = int(s)
    except ValueError:
        return 0, False
    if n <= 0:
        return 0, False
    return n, True
