from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smart_bridge.api import create_app
from smart_bridge.settings import Settings


def _client(checks: dict[str, str]) -> TestClient:
    def make(v: str):  # type: ignore[no-untyped-def]
        async def check() -> str:
            if v == "raise":
                raise RuntimeError
            return v

        return check

    app = create_app(Settings.from_env({}), {k: make(v) for k, v in checks.items()})
    return TestClient(app)


def test_healthz() -> None:
    assert _client({}).get("/healthz").json() == {"status": "ok"}


def test_readyz_ok() -> None:
    assert _client({"ha": "ok", "qdrant": "ok"}).get("/readyz").json()["status"] == "ok"


def test_qdrant_down_is_only_degraded() -> None:
    r = _client({"ha": "ok", "qdrant": "down"}).get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "degraded"


def test_ha_down_is_503_and_exception_is_down() -> None:
    assert _client({"ha": "down"}).get("/readyz").status_code == 503
    assert _client({"mqtt": "raise"}).get("/readyz").status_code == 503


def test_app_factory_without_adapters_serves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from smart_bridge.api import app_factory

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for var in ("HA_URL", "HA_TOKEN", "MQTT_HOST"):
        monkeypatch.delenv(var, raising=False)
    with TestClient(app_factory()) as client:
        assert client.get("/api/state").json() == {}
        assert client.get("/readyz").json()["status"] == "ok"


def test_webhook_trigger_reaches_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from smart_bridge.api import app_factory

    rules = tmp_path / "config" / "rules"
    rules.mkdir(parents=True)
    (rules / "r.yaml").write_text(
        "rules: [{id: w, triggers: [{type: webhook, webhook_id: abc}], "
        "actions: [{type: notify, message: hi}]}]"
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for var in ("HA_URL", "HA_TOKEN", "MQTT_HOST"):
        monkeypatch.delenv(var, raising=False)
    with TestClient(app_factory()) as client:
        assert client.post("/api/webhooks/abc").status_code == 202
        assert client.post("/api/webhooks/unknown").status_code == 404
