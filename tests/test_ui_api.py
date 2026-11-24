from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import Mount

import smart_bridge.ui as ui_pkg
from smart_bridge.api import app_factory
from smart_bridge.rules import parse_rules_text
from smart_bridge.ui import UiReader, build_ui_router
from smart_bridge.ui.routes import event_stream

from .test_mcp_server import running_bridge
from .test_rulegraph import BIG

NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)
LAMP = """rules:
- id: lamp
  intent: "Licht an"
  tags: [licht]
  created_by: claude
  triggers: [{type: state, entity_id: binary_sensor.door, to: "on"}]
  actions: [{type: ha_service, service: light.turn_on, target: {entity_id: light.hof}}]
- id: rain
  title: Regen
  alert: {target: zone.hof, severity: warning}
  condition: {numeric_state: {entity_id: sensor.rain, above: 0.1}}
  status: shadow
"""


class Fake:
    def __init__(self) -> None:
        rules = [*parse_rules_text(LAMP)[0], *parse_rules_text(BIG)[0]]
        self.rules = {r.id: r for r in rules}
        self.run_rows: list[dict[str, Any]] = [
            {
                "run_id": "r2",
                "rule_id": "lamp",
                "phase": "actions",
                "result": "ok",
                "started_at": "2026-01-10T11:00:00+00:00",
                "trigger": {"type": "state", "index": 0},
                "condition_trace": {"kind": "rule", "result": True, "detail": "", "children": []},
                "actions": [{"path": "0", "status": "ok"}],
            },
            {
                "run_id": "r1",
                "rule_id": "lamp",
                "phase": "actions",
                "result": "error",
                "started_at": "2026-01-09T11:00:00+00:00",
                "trigger": {},
                "condition_trace": {},
                "actions": [],
            },
        ]
        self.entities: dict[str, dict[str, Any]] = {
            "light.hof": {"name": "Hoflicht", "area": "Hof", "state": "off", "stale": False},
            "sensor.rain": {"name": "Regen", "area": None, "state": "0.0", "stale": False},
        }
        self.state_map = {"sensor.rain": {"state": "0.0", "stale": False}}
        self.alert_list: list[dict[str, Any]] = []

    def reader(self, search: Any = None) -> UiReader:
        def runs(rule_id: str | None, limit: int) -> list[dict[str, Any]]:
            return [r for r in self.run_rows if rule_id in (None, r["rule_id"])][:limit]

        def version_text(rule_id: str, version: int) -> str:
            if version > 2:
                raise LookupError("no such version")
            return f"id: {rule_id}\nv: {version}\n"

        return UiReader(
            rules=lambda: self.rules,
            tier=lambda r: 1 if r.actions else 0,
            entity=lambda e: self.entities.get(e),
            owner=lambda rid: rid.split("__")[0],
            alert_active=lambda rid: rid == "rain",
            runs=runs,
            run_counts=lambda since: [("lamp", "2026-01-10", 2), ("lamp__x", "2026-01-09", 1)],
            versions=lambda rid: [{"version": 1}, {"version": 2}],
            version_text=version_text,
            rule_yaml=lambda rid: f"id: {rid}\n",
            states=lambda: self.state_map,
            alerts=lambda: self.alert_list,
            now=lambda: NOW,
            search=search,
        )


def client(fake: Fake, token: str | None = None, search: Any = None) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(build_ui_router(fake.reader(search), token))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_rule_list_filters_and_summary() -> None:
    async with client(Fake()) as c:
        rows = {r["id"]: r for r in (await c.get("/ui/api/rules")).json()["rules"]}
        assert set(rows) == {"lamp", "rain", "big"}
        assert rows["lamp"]["tier"] == 1
        assert rows["lamp"]["last_fired"] == "2026-01-10T11:00:00+00:00"
        assert rows["rain"]["alert_active"] is True and rows["rain"]["status"] == "shadow"
        assert rows["lamp"]["areas"] == ["Hof"]

        async def ids(**params: Any) -> list[str]:
            data = (await c.get("/ui/api/rules", params=params)).json()["rules"]
            return [r["id"] for r in data]

        assert await ids(tag="licht") == ["lamp"]
        assert await ids(status="shadow") == ["rain"]
        assert await ids(created_by="claude") == ["lamp"]
        assert await ids(tier=0) == ["rain"]
        assert await ids(area="Hof") == ["big", "lamp"]
        assert await ids(q="regen") == ["rain"]  # Text-Fallback ohne Katalog
        assert (await c.get("/ui/api/rules", params={"tier": 7})).status_code == 422


async def test_semantic_search_ranks_and_falls_back_when_catalog_fails() -> None:
    async def search(q: str, limit: int) -> list[str]:
        return ["rain", "lamp"]

    async def broken(q: str, limit: int) -> list[str]:
        raise RuntimeError("qdrant down")

    async with client(Fake(), search=search) as c:
        data = (await c.get("/ui/api/rules", params={"q": "nass"})).json()
        assert data["semantic"] is True and [r["id"] for r in data["rules"]] == ["rain", "lamp"]
    async with client(Fake(), search=broken) as c:
        data = (await c.get("/ui/api/rules", params={"q": "regen"})).json()
        assert data["semantic"] is False and [r["id"] for r in data["rules"]] == ["rain"]


async def test_detail_graph_runs_versions_and_diff() -> None:
    async with client(Fake()) as c:
        detail = (await c.get("/ui/api/rules/lamp")).json()
        assert detail["yaml"] == "id: lamp\n" and detail["intent"] == "Licht an"
        assert detail["entities"]["trigger"] == ["binary_sensor.door"]
        assert detail["entities"]["action"] == ["light.hof"]

        graph = (await c.get("/ui/api/rules/lamp/graph")).json()
        assert {"nodes", "edges", "layout_hints"} <= set(graph) and "run" not in graph
        assert graph["tier"] == 1 and graph["status"] == "active"

        traced = (await c.get("/ui/api/rules/lamp/graph", params={"run_id": "r2"})).json()
        assert "trigger:0" in traced["run"]["highlight"]["nodes"]
        assert (await c.get("/ui/api/rules/lamp/graph?run_id=nope")).status_code == 404

        runs = (await c.get("/ui/api/rules/lamp/runs", params={"limit": 1})).json()["runs"]
        assert [r["run_id"] for r in runs] == ["r2"]
        assert (await c.get("/ui/api/rules/lamp/versions")).json()["versions"][1]["version"] == 2
        diff = (await c.get("/ui/api/rules/lamp/diff", params={"from": 1, "to": 2})).json()
        assert "-v: 1" in diff["diff"] and "+v: 2" in diff["diff"]
        assert (
            await c.get("/ui/api/rules/lamp/diff", params={"from": 1, "to": 9})
        ).status_code == 404
        for path in ("", "/graph", "/runs", "/versions"):
            assert (await c.get(f"/ui/api/rules/ghost{path}")).status_code == 404


async def test_device_view_lists_roles() -> None:
    async with client(Fake()) as c:
        data = (await c.get("/ui/api/entities/light.hof/rules")).json()
        roles = {r["id"]: r["roles"] for r in data["rules"]}
        assert roles == {"lamp": ["action"], "big": ["action"]}
        assert data["entity"]["name"] == "Hoflicht"
        rain = (await c.get("/ui/api/entities/sensor.rain/rules")).json()["rules"]
        assert {r["id"]: r["roles"] for r in rain}["rain"] == ["condition", "trigger"]
        assert (await c.get("/ui/api/entities/sensor.unknown/rules")).status_code == 404
        listing = {e["entity_id"]: e for e in (await c.get("/ui/api/entities")).json()["entities"]}
        assert listing["light.hof"]["rule_count"] == 2 and listing["light.hof"]["state"] == "off"


async def test_overview_groups_heatmap_and_highlights() -> None:
    async with client(Fake()) as c:
        data = (await c.get("/ui/api/overview")).json()
        assert data["groups"]["area"]["Hof"] == ["lamp", "big"]
        assert data["groups"]["tag"]["licht"] == ["lamp"]
        assert data["groups"]["target"]["zone.hof"] == ["rain"]
        assert data["heatmap"]["rules"] == {"lamp": {"2026-01-10": 2, "2026-01-09": 1}}
        assert data["highlight"]["shadow"] == ["rain"] and data["highlight"]["error"] == []


async def test_optional_token_protects_ui_api() -> None:
    async with client(Fake(), token="tok") as c:
        assert (await c.get("/ui/api/rules")).status_code == 401
        assert (
            await c.get("/ui/api/rules", headers={"Authorization": "Bearer x"})
        ).status_code == 401
        assert (
            await c.get("/ui/api/rules", headers={"Authorization": "Bearer tok"})
        ).status_code == 200
        assert (await c.get("/ui/api/rules?token=tok")).status_code == 200  # EventSource


async def test_event_stream_emits_changes_only() -> None:
    fake = Fake()
    frames: list[tuple[str, Any]] = []
    ticks = 0
    async for raw in event_stream(fake.reader(), interval=0, limit=3):
        event, data = raw.strip().split("\n")
        frames.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
        if event == "event: alerts":
            ticks += 1
            if ticks == 1:  # nach dem ersten Durchlauf ändert sich etwas
                fake.state_map["sensor.rain"] = {"state": "3.0", "stale": False}
                fake.state_map["sensor.ignored"] = {"state": "x", "stale": False}
                fake.run_rows.insert(0, {**fake.run_rows[0], "run_id": "r3"})
                fake.alert_list.append({"id": "a"})
    kinds = [k for k, _ in frames]
    assert kinds == ["hello", "state", "alerts", "state", "run", "alerts"]
    assert frames[1][1] == {"entity_id": "sensor.rain", "state": "0.0", "stale": False}
    assert frames[3][1]["state"] == "3.0"  # sensor.ignored kommt in keiner Regel vor
    assert frames[4][1]["run_id"] == "r3" and frames[4][1]["rule"] == "lamp"
    assert frames[5][1] == [{"id": "a"}]


# --- Read-only garantiert -----------------------------------------------------------------------------


def iter_routes(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, set[str]]]:
    """Alle (Pfad, Methoden) inkl. eingebundener Router und Mounts."""
    for route in routes:
        if (inner := getattr(route, "original_router", None)) is not None:
            yield from iter_routes(inner.routes, prefix)
        elif isinstance(route, Mount):
            yield from iter_routes(route.routes, prefix + route.path)
        elif getattr(route, "methods", None) is None:  # WebSocket/Mount ohne Methoden
            yield prefix + route.path, {"WEBSOCKET"}
        else:
            yield prefix + route.path, set(route.methods)


def ui_routes(app: FastAPI) -> list[tuple[str, set[str]]]:
    return [(p, m) for p, m in iter_routes(app.routes) if p == "/ui" or p.startswith("/ui/")]


def test_all_ui_routes_are_get_or_head_only() -> None:
    app = FastAPI()
    app.include_router(build_ui_router(Fake().reader()))
    routes = ui_routes(app)
    assert len(routes) >= 8
    for path, methods in routes:
        assert methods <= {"GET", "HEAD"}, (path, methods)


async def test_real_app_ui_routes_read_only_and_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    (config / "rules").mkdir(parents=True)
    (config / "rules" / "r.yaml").write_text(
        "rules:\n- id: regen_alert\n  intent: Warne\n  alert: {target: zone.hof, severity: warning}\n"
        "  condition: {numeric_state: {entity_id: sensor.regen_rate, above: 0.1}}\n"
    )
    for k, v in {"CONFIG_DIR": str(config), "DATA_DIR": str(tmp_path / "data")}.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "data").mkdir()
    for var in ("HA_URL", "HA_MCP_URL", "MQTT_HOST", "QDRANT_URL", "MCP_SERVER_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    app = app_factory()
    assert ui_routes(app)
    for path, methods in ui_routes(app):
        assert methods <= {"GET", "HEAD"}, (path, methods)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for method in ("post", "put", "patch", "delete"):
            r = await getattr(c, method)("/ui/api/rules")
            assert r.status_code == 405, method
        assert (await c.get("/ui/api/overview")).status_code == 200


def test_ui_package_has_no_write_path_imports() -> None:
    forbidden = ("executor", "mcp_server", "approvals", "authoring", "adapters")
    for path in Path(ui_pkg.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [f"{'.' * node.level}{node.module or ''}"]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                assert not any(part in forbidden for part in name.strip(".").split(".")), (
                    path.name,
                    name,
                )


async def test_graph_endpoint_against_running_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_bridge(tmp_path, monkeypatch) as (url, _), httpx.AsyncClient() as c:
        async with asyncio.timeout(10):  # Engine-Start abwarten
            while not (await c.get(f"{url}/ui/api/rules")).json()["rules"]:
                await asyncio.sleep(0.1)
        rows = (await c.get(f"{url}/ui/api/rules")).json()["rules"]
        assert [(r["id"], r["tier"], r["status"]) for r in rows] == [("regen_alert", 0, "active")]
        graph = (await c.get(f"{url}/ui/api/rules/regen_alert/graph")).json()
        nodes = {n["id"]: n for n in graph["nodes"]}
        assert nodes["alert"]["data"]["target"] == "zone.hof"
        assert nodes["entity:sensor.regen_rate"]["data"]["state"] == "0.0"  # Live-Zustand
        assert (await c.get(f"{url}/ui/api/rules/regen_alert")).json()["yaml"].startswith("rules:")
        assert (await c.post(f"{url}/ui/api/rules")).status_code == 405
