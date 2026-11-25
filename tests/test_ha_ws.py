import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from smart_bridge.adapters.ha_ws import HaAuthError, HaWebSocket
from smart_bridge.backoff import Backoff
from smart_bridge.events import BridgeEvent
from smart_bridge.metrics import BridgeMetrics
from smart_bridge.state import StateStore


def _state(eid: str, state: str) -> dict[str, Any]:
    return {
        "entity_id": eid,
        "state": state,
        "attributes": {},
        "last_changed": "2026-01-01T12:00:00+00:00",
    }


class FakeHa:
    """Minimaler HA-WebSocket. `scripts[n]` beschreibt Verbindung n."""

    def __init__(self) -> None:
        self.connections = 0
        self.states: list[list[dict[str, Any]]] = []  # je Verbindung
        self.push_after_sync: list[list[dict[str, Any]]] = []  # Events je Verbindung
        self.drop_after_sync = True
        self.token = "good"
        self.service_calls: list[dict[str, Any]] = []

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        n = self.connections
        self.connections += 1
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth["access_token"] != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "bad"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        sub_id = 0
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            cmd = msg.json()
            kind = cmd["type"]
            if kind == "subscribe_events":
                if cmd["event_type"] == "state_changed":
                    sub_id = cmd["id"]
                await ws.send_json({"id": cmd["id"], "type": "result", "success": True})
            elif kind == "get_states":
                data = self.states[min(n, len(self.states) - 1)]
                await ws.send_json(
                    {"id": cmd["id"], "type": "result", "success": True, "result": data}
                )
                for ev in self.push_after_sync[n] if n < len(self.push_after_sync) else []:
                    await ws.send_json({"id": sub_id, "type": "event", "event": ev})
                if self.drop_after_sync and n == 0:
                    await ws.close()
            elif kind == "call_service":
                self.service_calls.append(cmd)
                await ws.send_json({"id": cmd["id"], "type": "result", "success": True})
        return ws


@pytest.fixture
async def ha() -> AsyncIterator[tuple[FakeHa, TestServer]]:
    fake = FakeHa()
    app = web.Application()
    app.router.add_get("/ws", fake.handler)
    server = TestServer(app)
    await server.start_server()
    yield fake, server
    await server.close()


def _adapter(
    server: TestServer,
    store: StateStore,
    events: list[BridgeEvent],
    token: str = "good",
    metrics: BridgeMetrics | None = None,
) -> HaWebSocket:
    async def sink(e: BridgeEvent) -> None:
        events.append(e)

    async def fast_sleep(_: float) -> None:
        await asyncio.sleep(0)

    return HaWebSocket(
        str(server.make_url("/ws")).replace("http", "ws"),
        token,
        store,
        sink,
        events=("state_changed", "haus3d_targets"),
        backoff=Backoff(0.0, 0.0),
        sleep=fast_sleep,
        command_timeout=2,
        metrics=metrics,
    )


async def test_full_sync_events_stale_and_reconnect(ha: tuple[FakeHa, TestServer]) -> None:
    fake, server = ha
    fake.states = [[_state("light.a", "on")], [_state("light.a", "on"), _state("light.b", "off")]]
    fake.push_after_sync = [
        [
            {
                "event_type": "state_changed",
                "data": {"entity_id": "light.a", "new_state": _state("light.a", "off")},
            },
            {"event_type": "haus3d_targets", "data": {"version": 1}},
        ]
    ]
    store = StateStore()
    events: list[BridgeEvent] = []
    metrics = BridgeMetrics()
    changes: list[tuple[str, bool]] = []

    async def listener(c) -> None:  # type: ignore[no-untyped-def]
        changes.append((c.entity_id, c.state_changed))

    store.subscribe(listener)
    adapter = _adapter(server, store, events, metrics=metrics)
    task = asyncio.create_task(adapter.run())
    try:
        # Nach Verbindung 1 (dann Abbruch) und Verbindung 2 ist alles synchron
        async with asyncio.timeout(5):
            while fake.connections < 2 or not adapter.synced.is_set():
                await asyncio.sleep(0.01)
        assert events == [BridgeEvent("ha", "haus3d_targets", {"version": 1})]
        assert store.get("light.a").state == "on"  # type: ignore[union-attr]
        assert not store.get("light.a").stale  # type: ignore[union-attr]
        assert store.get("light.b") is not None
        # Der Verbindungsabbruch selbst erzeugte keine Zustandsänderung für light.a
        assert changes.count(("light.a", True)) == 3  # initial, ->off, ->on nach Reconnect
        await adapter.call_service("light", "turn_on", target={"entity_id": "light.a"})
        assert fake.service_calls[0]["service"] == "turn_on"
        payload, _ = metrics.scrape()
        assert b"smart_bridge_ha_connection_attempts_total 2.0" in payload
        assert b"smart_bridge_ha_reconnects_total 1.0" in payload
        assert b"smart_bridge_ha_events_total 2.0" in payload
    finally:
        task.cancel()


async def test_stale_while_disconnected(ha: tuple[FakeHa, TestServer]) -> None:
    fake, server = ha
    fake.states = [[_state("light.a", "on")]]
    fake.drop_after_sync = True
    store = StateStore()

    gate = asyncio.Event()

    async def blocked_sleep(_: float) -> None:
        gate.set()
        await asyncio.Event().wait()

    adapter = _adapter(server, store, [])
    adapter._sleep = blocked_sleep
    task = asyncio.create_task(adapter.run())
    try:
        async with asyncio.timeout(5):
            await gate.wait()
        assert not adapter.connected
        assert store.get("light.a").stale  # type: ignore[union-attr]
    finally:
        task.cancel()


async def test_invalid_token_is_fatal(ha: tuple[FakeHa, TestServer]) -> None:
    _, server = ha
    adapter = _adapter(server, StateStore(), [], token="bad")
    with pytest.raises(HaAuthError):
        await asyncio.wait_for(adapter.run(), 5)


async def test_timed_out_command_is_removed_from_pending() -> None:
    class NoReplySocket:
        async def send_json(self, _: dict[str, Any]) -> None:
            return None

    adapter = HaWebSocket("ws://unused", "token", StateStore(), _noop, command_timeout=0.01)
    adapter._ws = NoReplySocket()  # type: ignore[assignment]
    with pytest.raises(TimeoutError):
        await adapter.command({"type": "get_states"})
    assert adapter._pending == {}


async def _noop(_: BridgeEvent) -> None:
    return None
