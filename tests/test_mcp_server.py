from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent
from pydantic import AnyUrl

from smart_bridge.api import app_factory
from smart_bridge.history import StateHistory
from smart_bridge.mcp_server.examples import RULE_EXAMPLES
from smart_bridge.rules import parse_rules_text

from .mock_server import running_mock

TOKEN = "s3cret-mcp-token"
RULES = """rules:
- id: regen_alert
  intent: "Warne mich bei Regen"
  alert: {target: zone.hof, severity: warning}
  condition: {numeric_state: {entity_id: sensor.regen_rate, above: 0.1}}
"""
BACKTEST_RULE = """rules:
- id: bt
  alert: {target: battery, severity: info}
  condition: {numeric_state: {entity_id: sensor.regen_rate, above: 1}}
- id: bt_act
  triggers: [{type: state, entity_id: sensor.regen_rate, to: "5"}]
  actions: [{type: ha_service, service: light.turn_on, target: {entity_id: light.aussen_hof}}]
"""


def test_examples_are_valid_rules() -> None:
    rules, errors = parse_rules_text(RULE_EXAMPLES)
    assert not errors and len(rules) == 4


@asynccontextmanager
async def running_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None = TOKEN
) -> AsyncIterator[tuple[str, Path]]:
    config = tmp_path / "config"
    (config / "rules").mkdir(parents=True)
    (config / "rules" / "r.yaml").write_text(RULES)
    (config / "policy.yaml").write_text("tiers: {}\n")
    data = tmp_path / "data"
    data.mkdir()
    now = datetime.now(UTC)
    hist = StateHistory(data / "bridge.db", retention_days=14)
    for delta, state in ((-5, "0"), (-3, "5"), (-2, "0")):
        hist.record("sensor.regen_rate", state, now + timedelta(days=delta))
    hist.close()
    async with running_mock("tok") as mock:
        env = {
            "DATA_DIR": str(data),
            "CONFIG_DIR": str(config),
            "HA_URL": mock.ws_url,
            "HA_TOKEN": "tok",
            "QDRANT_URL": ":memory:",
            "EMBED_MODEL": "hash",
            "MCP_SERVER_ENABLED": "true",
        }
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        if token:
            monkeypatch.setenv("MCP_SERVER_TOKEN", token)
        else:
            monkeypatch.delenv("MCP_SERVER_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_URL", raising=False)
        monkeypatch.delenv("MQTT_HOST", raising=False)
        server = uvicorn.Server(
            uvicorn.Config(app_factory(), host="127.0.0.1", port=0, log_level="warning")
        )
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}", data
        finally:
            server.should_exit = True
            await task


def _text(result: Any) -> Any:
    assert not result.isError, result.content
    block = result.content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)


async def _call(s: ClientSession, name: str, **args: Any) -> Any:
    res = await s.call_tool(name, args)
    if res.isError:
        return res
    if res.structuredContent is not None:
        content = res.structuredContent
        return content.get("result", content)
    return _text(res)


async def test_requires_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with running_bridge(tmp_path, monkeypatch) as (url, _):
        async with httpx.AsyncClient() as c:
            body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            assert (await c.post(f"{url}/mcp", json=body)).status_code == 401
            bad = {"Authorization": "Bearer nope"}
            assert (await c.post(f"{url}/mcp", json=body, headers=bad)).status_code == 401
            assert (await c.get(f"{url}/healthz")).status_code == 200  # Rest bleibt offen


async def test_enabled_without_token_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeError, match="MCP_SERVER_TOKEN"):
        async with running_bridge(tmp_path, monkeypatch, token=None):
            pass


async def test_read_tools_resources_prompt_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_bridge(tmp_path, monkeypatch) as (url, data):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with (
            streamablehttp_client(f"{url}/mcp", headers=headers) as (r, w, _),
            ClientSession(r, w) as s,
        ):
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            assert tools == {
                "search_entities", "get_state", "list_alerts", "list_rules", "search_rules",
                "list_model_targets", "explain_rule", "simulate_rule", "backtest_rule",
                "list_rule_versions",
            }  # fmt: skip

            async with asyncio.timeout(10):  # Katalog-Sync nach Engine-Start
                while True:
                    found = await _call(s, "search_entities", query="Regen")
                    if isinstance(found, list) and found:
                        break
                    await asyncio.sleep(0.1)
            hits = await _call(s, "search_entities", query="Regen", domain="sensor")
            assert hits[0]["entity_id"] == "sensor.regen_rate"

            state = await _call(s, "get_state", entity_id="sensor.regen_rate")
            assert state["entity_id"] == "sensor.regen_rate"
            assert (await _call(s, "get_state", entity_id="sensor.gibt_es_nicht")).isError

            rules = await _call(s, "list_rules")
            assert [x["id"] for x in rules] == ["regen_alert"]
            explained = await _call(s, "explain_rule", rule_id="regen_alert")
            assert "Warne mich bei Regen" in explained["text"]
            assert explained["evaluation"]["kind"] == "rule"
            assert (await _call(s, "explain_rule", rule_id="nope")).isError

            sim = await _call(s, "simulate_rule", rule_yaml=BACKTEST_RULE)
            assert sim["valid"] and sim["side_effects"] is False
            assert {x["rule_id"] for x in sim["rules"]} == {"bt", "bt_act"}
            at = (datetime.now(UTC) - timedelta(days=2, hours=12)).isoformat()
            past = await _call(s, "simulate_rule", rule_yaml=BACKTEST_RULE, at=at)
            assert past["source"] == "history"
            assert {x["rule_id"]: x["condition_true"] for x in past["rules"]}["bt"] is True
            early = (datetime.now(UTC) - timedelta(days=4)).isoformat()
            before = await _call(s, "simulate_rule", rule_yaml=BACKTEST_RULE, at=early)
            assert {x["rule_id"]: x["condition_true"] for x in before["rules"]}["bt"] is False
            bad = await _call(s, "simulate_rule", rule_yaml="rules: [{id: x}]")
            assert bad["valid"] is False and bad["errors"]

            bt = await _call(s, "backtest_rule", rule_yaml=BACKTEST_RULE, days=7)
            act = bt["rules"]["bt"]
            assert act["activation_count"] == 1
            assert act["total_active_seconds"] == 86400
            assert bt["rules"]["bt_act"]["fire_count"] == 1
            assert bt["rules"]["bt_act"]["fires"][0]["actions"][0]["service"] == "light.turn_on"

            assert await _call(s, "list_rule_versions", rule_id="regen_alert") == []
            assert (await _call(s, "list_model_targets"))["targets"] == []

            schema = await s.read_resource(AnyUrl("bridge://docs/rule-schema"))
            assert "rules" in json.loads(_resource_text(schema))["properties"]
            rule_res = await s.read_resource(AnyUrl("bridge://rules/regen_alert"))
            assert "regen_alert" in _resource_text(rule_res)
            policy = await s.read_resource(AnyUrl("bridge://policy"))
            assert "tiers" in _resource_text(policy)
            prompt = await s.get_prompt("author_rule", {"request": "Licht bei Regen"})
            assert "Licht bei Regen" in str(prompt.messages[0].content)

    rows = sqlite3.connect(data / "bridge.db").execute("SELECT action, details FROM audit_log")
    logged = rows.fetchall()
    assert {"mcp.explain_rule", "mcp.backtest_rule", "mcp.resource.policy"} <= {
        a for a, _ in logged
    }
    assert all(TOKEN not in d for _, d in logged)


def _resource_text(res: Any) -> str:
    return str(res.contents[0].text)
