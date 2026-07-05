class DocketError(Exception):
    """A real command failure: printed as `error: <msg>` to stderr and recorded
    in telemetry's `err` field (so `docket stats` counts it as an error). Exit 1.

    This is distinct from a command that intentionally exits non-zero WITHOUT a
    fault — e.g. `validate` finding data problems returns exit 1 with an empty
    `err`, so stats does NOT count it as a tool error (the Go version conflated
    the two; this rewrite separates them — observation F2)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ExitSignal(Exception):
    """Intentional non-zero exit that is NOT a tool fault. The wrapper exits with
    `.code` and records an EMPTY `err`, so `docket stats` does not count it as an
    error. Used by `validate`/`lint` when they find data problems (they already
    printed the problems to stderr)."""

    def __init__(self, code: int):
        super().__init__(f"exit {code}")
        self.code = code
