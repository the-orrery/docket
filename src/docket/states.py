"""State-type / status mapping, priority ranks, state resolution. Port of states.go."""

from .errors import DocketError

# state_type enum -> display status name, per ADR / observed frontmatter.
STATE_TYPE_TO_STATUS = {
    "backlog": "Backlog",
    "unstarted": "Todo",
    "started": "In Progress",
    "completed": "Done",
    "canceled": "Canceled",
}

# reverse: display status -> state_type.
STATUS_TO_STATE_TYPE = {v: k for k, v in STATE_TYPE_TO_STATUS.items()}

# priority display order for sorting (lower = higher priority).
PRIORITY_RANK = {
    "Urgent": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
    "No priority": 4,
}

# aliases for "No priority": bare "none", hyphenated, and spaced forms.
_NO_PRIORITY_ALIASES = {"none", "no-priority", "no priority"}


def valid_state_type(st: str) -> bool:
    return st in STATE_TYPE_TO_STATUS


def valid_priority(p: str) -> bool:
    return p in PRIORITY_RANK


def resolve_priority(arg: str) -> str:
    """Accept a priority string in any case (or a "no priority" alias) and
    return the canonical display form (Urgent/High/Medium/Low/No priority).
    Raises DocketError for unrecognised values."""
    arg = arg.strip()
    # exact match first (fast path, covers already-canonical values).
    if arg in PRIORITY_RANK:
        return arg
    # case-insensitive match against canonical names.
    lower = arg.lower()
    for canon in PRIORITY_RANK:
        if canon.lower() == lower:
            return canon
    # accept "none" / "no-priority" / "no priority" as aliases.
    if lower in _NO_PRIORITY_ALIASES:
        return "No priority"
    raise DocketError(
        f'invalid priority "{arg}" (want one of: Urgent/High/Medium/Low/"No priority")'
    )


def resolve_state(arg: str):
    """Accept either a state_type ("started") or a display status ("In Progress")
    and return the canonical (status, state_type) pair."""
    arg = arg.strip()
    if arg in STATE_TYPE_TO_STATUS:
        return STATE_TYPE_TO_STATUS[arg], arg
    if arg in STATUS_TO_STATE_TYPE:
        return arg, STATUS_TO_STATE_TYPE[arg]
    # case-insensitive status match (e.g. "in progress", "done")
    for disp, stt in STATUS_TO_STATE_TYPE.items():
        if disp.lower() == arg.lower():
            return disp, stt
    for stt, disp in STATE_TYPE_TO_STATUS.items():
        if stt.lower() == arg.lower():
            return disp, stt
    raise DocketError(
        f'unknown state "{arg}" (want one of: backlog/unstarted/started/'
        'completed/canceled or Backlog/Todo/"In Progress"/Done/Canceled)'
    )
