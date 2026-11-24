from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

DEFAULT_ENTITIES: dict[str, tuple[str, dict[str, Any]]] = {
    "light.aussen_hof": ("off", {"friendly_name": "Außenlicht Hof"}),
    "light.wohnzimmer": ("off", {"friendly_name": "Wohnzimmer"}),
    "lock.nuki_haustuer": ("unlocked", {"friendly_name": "Haustür"}),
    "sensor.regen_rate": ("0.0", {"friendly_name": "Regen", "unit_of_measurement": "mm/h"}),
}

_ON_OFF = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked"}


class MockHa:
    """Zustand, aufgezeichnete Aufrufe und Abonnenten; die Apps hängen daran."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.entities: dict[str, dict[str, Any]] = {
            eid: {"entity_id": eid, "state": st, "attributes": dict(attrs)}
            for eid, (st, attrs) in DEFAULT_ENTITIES.items()
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []  # ("service"|"tool"|"event", payload)
        self._subs: list[tuple[WebSocket, int, str | None]] = []
        self.fail_services = False
        self.registry: dict[str, list[dict[str, Any]]] = {
            "entity_registry": [
                {"entity_id": eid, "device_id": "dev_" + eid.split(".")[1], "area_id": None}
                for eid in self.entities
            ],
            "device_registry": [
                {"id": "dev_" + eid.split(".")[1], "name": eid.split(".")[1], "area_id": "flur"}
                for eid in self.entities
            ],
            "area_registry": [{"area_id": "flur", "name": "Flur"}],
        }

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def state_dicts(self) -> list[dict[str, Any]]:
        return [
            {**e, "last_changed": self._now(), "last_updated": self._now(), "context": {}}
            for e in self.entities.values()
        ]

    async def set_state(self, entity_id: str, state: str) -> None:
        old = self.entities.get(entity_id)
        new = {
            "entity_id": entity_id,
            "state": state,
            "attributes": (old or {}).get("attributes", {}),
        }
        self.entities[entity_id] = new
        stamp = {"last_changed": self._now(), "last_updated": self._now(), "context": {}}
        await self.emit(
            "state_changed",
            {
                "entity_id": entity_id,
                "old_state": {**old, **stamp} if old else None,
                "new_state": {**new, **stamp},
            },
        )

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        for ws, sub_id, wanted in list(self._subs):
            if wanted not in (None, event_type):
                continue
            try:
                await ws.send_json(
                    {
                        "id": sub_id,
                        "type": "event",
                        "event": {"event_type": event_type, "data": data, "origin": "LOCAL"},
                    }
                )
            except Exception:
                self._subs = [s for s in self._subs if s[0] is not ws]

    def find_by_name(self, name: str, domains: list[str] | None) -> str | None:
        for eid, e in self.entities.items():
            if e["attributes"].get("friendly_name", "").lower() == name.lower() and (
                not domains or eid.split(".")[0] in domains
            ):
                return eid
        return None

    async def apply_service(self, domain: str, service: str, entity_ids: list[str]) -> None:
        if self.fail_services:
            raise RuntimeError("mock failure")
        for eid in entity_ids:
            if eid not in self.entities:
                raise KeyError(eid)
            await self.set_state(eid, _ON_OFF.get(service, self.entities[eid]["state"]))


def _build_mcp(ha: MockHa, path: str) -> FastMCP:
    mcp = FastMCP(
        "mock-ha",
        streamable_http_path=path,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def intent(service: str, name: str, domain: list[str] | None) -> str:
        ha.calls.append(("tool", {"service": service, "name": name, "domain": domain}))
        eid = ha.find_by_name(name, domain)
        if eid is None:
            raise ValueError(f"no exposed entity named {name!r}")
        await ha.apply_service(eid.split(".")[0], service, [eid])
        return f"{eid} -> {ha.entities[eid]['state']}"

    @mcp.tool(name="HassTurnOn")
    async def hass_turn_on(name: str, domain: list[str] | None = None) -> str:
        """Turns on a device or entity."""
        return await intent("turn_on", name, domain)

    @mcp.tool(name="HassTurnOff")
    async def hass_turn_off(name: str, domain: list[str] | None = None) -> str:
        """Turns off a device or entity."""
        return await intent("turn_off", name, domain)

    @mcp.tool(name="GetLiveContext")
    async def get_live_context() -> str:
        """Current state of exposed entities."""
        return json.dumps({e: v["state"] for e, v in ha.entities.items()})

    return mcp


def create_app(ha: MockHa | None = None, mcp_path: str = "/api/mcp") -> Starlette:
    ha = ha or MockHa()
    app: Starlette = _build_mcp(ha, mcp_path).streamable_http_app()

    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"type": "auth_required", "ha_version": "mock"})
        auth = await ws.receive_json()
        if ha.token is not None and auth.get("access_token") != ha.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return
        await ws.send_json({"type": "auth_ok", "ha_version": "mock"})
        try:
            while True:
                msg = await ws.receive_json()
                await _handle(ha, ws, msg)
        except WebSocketDisconnect:
            ha._subs = [s for s in ha._subs if s[0] is not ws]  # noqa: SLF001

    app.router.routes.append(WebSocketRoute("/api/websocket", websocket))
    app.state.mock_ha = ha
    return app


async def _handle(ha: MockHa, ws: WebSocket, msg: dict[str, Any]) -> None:
    mid, kind = msg["id"], msg["type"]

    async def ok(result: Any = None) -> None:
        await ws.send_json({"id": mid, "type": "result", "success": True, "result": result})

    if kind == "subscribe_events":
        ha._subs.append((ws, mid, msg.get("event_type")))  # noqa: SLF001
        await ok()
    elif kind == "get_states":
        await ok(ha.state_dicts())
    elif kind == "call_service":
        ha.calls.append(("service", {k: v for k, v in msg.items() if k not in ("id", "type")}))
        target = msg.get("target") or {}
        raw = target.get("entity_id") or (msg.get("service_data") or {}).get("entity_id") or []
        try:
            await ha.apply_service(
                msg["domain"], msg["service"], [raw] if isinstance(raw, str) else raw
            )
        except Exception as exc:
            await ws.send_json(
                {
                    "id": mid,
                    "type": "result",
                    "success": False,
                    "error": {"code": "failed", "message": str(exc)},
                }
            )
        else:
            await ok()
    elif kind == "fire_event":
        ha.calls.append(("event", {"type": msg["event_type"], "data": msg.get("event_data")}))
        await ha.emit(msg["event_type"], msg.get("event_data") or {})
        await ok()
    elif kind in (
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
    ):
        await ok(ha.registry[kind.split("/")[1]])
    elif kind == "ping":
        await ws.send_json({"id": mid, "type": "pong"})
    else:
        await ws.send_json(
            {
                "id": mid,
                "type": "result",
                "success": False,
                "error": {"code": "unknown_command", "message": kind},
            }
        )
