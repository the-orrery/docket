from docket import cli
from docket.config import Settings


def test_settings_defaults() -> None:
    s = Settings()
    assert s.debug is False


def test_consume_tier_exports_active_tier(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "_load_tiers", lambda: {"secondary": "/tmp/secondary-docket"}
    )

    argv = cli._consume_tier(["--tier", "secondary", "finish", "ISSUE-1"])

    assert argv == ["finish", "ISSUE-1"]
    assert cli.os.environ["DOCKET_ROOT"] == "/tmp/secondary-docket"
    assert cli.os.environ["DOCKET_ACTIVE_TIER"] == "secondary"
