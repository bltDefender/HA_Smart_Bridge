"""HA-WebSocket-Adapter: Auth, Vollsync, Subscriptions, Reconnect mit Backoff."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

import aiohttp

from ..backoff import Backoff
from ..events import BridgeEvent, EventSink
from ..metrics import BridgeMetrics
from ..state import EntityState, StateStore

log = logging.getLogger(__name__)

DEFAULT_EVENTS = (
    "state_changed",
    "entity_registry_updated",
    "device_registry_updated",
    "area_registry_updated",
    "haus3d_targets",
)


class HaAuthError(Exception):
    """Token ungültig: Retry hilft nicht."""


class HaConnectionError(Exception):
    pass


def _entity_state(raw: dict[str, Any]) -> EntityState:
    return EntityState(
        entity_id=raw["entity_id"],
        state=str(raw["state"]),
        attributes=dict(raw.get("attributes") or {}),
        last_changed=datetime.fromisoformat(raw["last_changed"]),
    )


class HaWebSocket:
    def __init__(
        self,
        url: str,
        token: str,
        store: StateStore,
        sink: EventSink,
        *,
        events: Sequence[str] = DEFAULT_EVENTS,
        backoff: Backoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        command_timeout: float = 10.0,
        metrics: BridgeMetrics | None = None,
    ) -> None:
        self._url, self._token = url, token
        self._store, self._sink = store, sink
        self._events = tuple(events)
        self._backoff = backoff or Backoff()
        self._sleep = sleep
        self._timeout = command_timeout
        self._metrics = metrics
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._buffer: list[dict[str, Any]] | None = None  # Events während des Vollsync
        self.connected = False
        self.synced = asyncio.Event()

    # --- Lebenszyklus ---------------------------------------------------

    async def run(self) -> None:
        """Läuft bis zum Abbruch; verbindet nach Fehlern mit Backoff neu."""
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    if self._metrics:
                        self._metrics.inc("ha_connection_attempts")
                    await self._session(session)
                except HaAuthError:
                    log.error("HA authentication failed; not retrying")
                    raise
                except Exception as exc:  # jeder andere Fehler: neu verbinden statt sterben
                    log.warning("HA connection lost: %r", exc)
                    if self._metrics:
                        self._metrics.inc("ha_reconnects")
                finally:
                    self._on_disconnect()
                await self._sleep(self._backoff.next_delay())

    def _on_disconnect(self) -> None:
        self.connected = False
        self.synced.clear()
        self._ws = None
        self._buffer = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(HaConnectionError("disconnected"))
        self._pending.clear()
        self._store.mark_all_stale()

    async def _session(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self._url, heartbeat=30) as ws:
            self._ws = ws
            await self._authenticate(ws)
            self.connected = True
            reader = asyncio.create_task(self._read(ws))
            try:
                self._buffer = []
                for event_type in self._events:
                    await self._command({"type": "subscribe_events", "event_type": event_type})
                raw_states = await self._command({"type": "get_states"})
                await self._store.replace_all(_entity_state(s) for s in raw_states)
                buffered, self._buffer = self._buffer, None
                for msg in buffered:
                    await self._handle_event(msg)
                self.synced.set()
                self._backoff.reset()
                await reader
                raise HaConnectionError("connection closed")
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        first = await asyncio.wait_for(ws.receive_json(), self._timeout)
        if first.get("type") != "auth_required":
            raise HaConnectionError(f"unexpected greeting: {first.get('type')}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        reply = await asyncio.wait_for(ws.receive_json(), self._timeout)
        if reply.get("type") == "auth_invalid":
            raise HaAuthError(reply.get("message", "auth_invalid"))
        if reply.get("type") != "auth_ok":
            raise HaConnectionError(f"unexpected auth reply: {reply.get('type')}")

    # --- Nachrichten ----------------------------------------------------

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = msg.json()
            kind = data.get("type")
            if kind == "result":
                fut = self._pending.pop(data["id"], None)
                if fut and not fut.done():
                    if data.get("success"):
                        fut.set_result(data.get("result"))
                    else:
                        fut.set_exception(HaConnectionError(str(data.get("error"))))
            elif kind == "event":
                if self._metrics:
                    self._metrics.inc("ha_events")
                if self._buffer is not None:
                    self._buffer.append(data)
                else:
                    await self._handle_event(data)

    async def _handle_event(self, msg: dict[str, Any]) -> None:
        event = msg["event"]
        etype, data = event["event_type"], event.get("data") or {}
        if etype == "state_changed":
            new = data.get("new_state")
            if new is None:
                self._store.remove(data["entity_id"])
            else:
                st = _entity_state(new)
                await self._store.update(st.entity_id, st.state, st.attributes, st.last_changed)
            return
        await self._sink(BridgeEvent("ha", etype, data))

    async def _command(self, payload: dict[str, Any]) -> Any:
        ws = self._ws
        if ws is None:
            raise HaConnectionError("not connected")
        self._next_id += 1
        cid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            await ws.send_json({"id": cid, **payload})
            return await asyncio.wait_for(fut, self._timeout)
        finally:
            self._pending.pop(cid, None)
            if not fut.done():
                fut.cancel()

    # --- Ausgehend ------------------------------------------------------

    async def command(self, payload: dict[str, Any]) -> Any:
        """Beliebiges WS-Kommando (z. B. Registry-Abfragen)."""
        return await self._command(payload)

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service}
        if service_data:
            payload["service_data"] = service_data
        if target:
            payload["target"] = target
        return await self._command(payload)

    async def fire_event(self, event_type: str, event_data: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"type": "fire_event", "event_type": event_type}
        if event_data:
            payload["event_data"] = event_data
        return await self._command(payload)
