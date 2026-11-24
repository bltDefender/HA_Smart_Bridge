from pathlib import Path

import pytest

from smart_bridge.settings import Settings


def test_defaults() -> None:
    s = Settings.from_env({})
    assert s.executor == "mcp" and s.tz == "Europe/Berlin" and not s.dry_run


def test_secret_from_file(tmp_path: Path) -> None:
    f = tmp_path / "tok"
    f.write_text("abc\n")
    s = Settings.from_env({"HA_TOKEN_FILE": str(f), "DRY_RUN": "true"})
    assert s.ha_token == "abc" and s.dry_run


@pytest.mark.parametrize("env", [{"EXECUTOR": "x"}, {"TZ": "Mars/Base"}])
def test_invalid_env(env: dict[str, str]) -> None:
    with pytest.raises(Exception):  # noqa: B017
        Settings.from_env(env)
