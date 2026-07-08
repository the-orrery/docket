"""Configured docket tiers."""

from __future__ import annotations

from pathlib import Path


def load_tiers() -> dict[str, str]:
    path = Path.home() / ".config/docket/tiers.toml"
    if not path.exists():
        return {}
    import tomllib

    with path.open("rb") as f:
        data = tomllib.load(f)
    tiers = data.get("tiers", {})
    if not isinstance(tiers, dict):
        return {}
    return {str(k): str(Path(str(v)).expanduser()) for k, v in tiers.items()}
