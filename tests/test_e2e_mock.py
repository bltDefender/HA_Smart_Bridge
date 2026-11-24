from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from smart_bridge.api import app_factory

from .mock_server import running_mock

RULE = """rules:
- id: regen_licht
  triggers: [{type: state, entity_id: sensor.regen_rate, to: "1.0"}]
  actions:
  - {type: ha_service, service: light.turn_on, target: {entity_id: light.aussen_hof}}
- id: kaputt
  triggers: [{type: state, entity_id: sensor.regen_rate, to: "9"}]
  actions: [{type: ha_tool, tool: GibtEsNicht, arguments: {}}]
"""


async def _until(pred: Callable[[], object]) -> None:
    async with asyncio.timeout(5):
        while not pred():
            await asyncio.sleep(0.02)


async def test_state_change_runs_action_via_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = tmp_path / "config" / "rules"
    rules.mkdir(parents=True)
    (rules / "r.yaml").write_text(RULE)
    async with running_mock("tok") as mock:
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("HA_URL", mock.ws_url)
        monkeypatch.setenv("HA_TOKEN", "tok")
        monkeypatch.setenv("HA_MCP_URL", mock.mcp_url)
        monkeypatch.delenv("MQTT_HOST", raising=False)
        app = app_factory()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
        ):
            await _until(lambda: mock.ha._subs)  # noqa: SLF001
            await asyncio.sleep(0.5)  # Engine-Start nach Vollsync und Tool-Discovery
            await mock.ha.set_state("sensor.regen_rate", "1.0")
            await _until(lambda: mock.ha.entities["light.aussen_hof"]["state"] == "on")
            assert [k for k, _ in mock.ha.calls if k != "event"] == ["tool"]  # Standard: MCP

            runs: list[dict[str, str]] = []
            async with asyncio.timeout(5):
                while not runs:  # der Lauf wird erst nach dem letzten Schritt gespeichert
                    resp = await c.get("/api/runs", params={"rule_id": "regen_licht"})
                    runs = resp.json()["runs"]
                    await asyncio.sleep(0.02)
            assert runs and runs[0]["result"] == "ok" and runs[0]["executor"] == "mcp"

            alerts = (await c.get("/api/alerts")).json()["alerts"]
            assert any("GibtEsNicht" in str(a) for a in alerts)  # fehlendes Tool → Fehler-Alert

            assert (await c.post("/api/pause")).json() == {"paused": True}
            await mock.ha.set_state("light.aussen_hof", "off")
            await mock.ha.set_state("sensor.regen_rate", "0")
            await mock.ha.set_state("sensor.regen_rate", "1.0")
            await asyncio.sleep(0.3)
            assert mock.ha.entities["light.aussen_hof"]["state"] == "off"
            assert (await c.post("/api/resume")).json() == {"paused": False}
            assert (await c.get("/readyz")).json()["checks"]["ha_mcp"] == "ok"
