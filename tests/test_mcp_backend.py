from __future__ import annotations

import pytest

from smart_bridge.adapters.ha_ws import HaWebSocket
from smart_bridge.executor import (
    ActionError,
    Dispatcher,
    McpBackend,
    UnsupportedActionError,
    WsBackend,
)
from smart_bridge.executor.backends import map_service_to_tool
from smart_bridge.state import StateStore

from .mock_server import running_mock

NAMES = {"light.aussen_hof": "Außenlicht Hof", "light.wohnzimmer": "Wohnzimmer"}


async def test_discovery_and_tool_call_against_mock() -> None:
    async with running_mock() as mock:
        backend = McpBackend(mock.mcp_url, "t", NAMES.get)
        assert {"HassTurnOn", "HassTurnOff"} <= await backend.discover()
        await backend.call_tool("HassTurnOn", {"name": "Außenlicht Hof", "domain": ["light"]})
        assert mock.ha.entities["light.aussen_hof"]["state"] == "on"
        assert backend.available


async def test_service_is_mapped_to_intent() -> None:
    async with running_mock() as mock:
        backend = McpBackend(mock.mcp_url, None, NAMES.get)
        await backend.discover()
        await backend.call_service("light", "turn_on", None, {"entity_id": "light.wohnzimmer"})
        assert mock.ha.calls == [
            ("tool", {"service": "turn_on", "name": "Wohnzimmer", "domain": ["light"]})
        ]


async def test_tool_error_and_unknown_tool() -> None:
    async with running_mock() as mock:
        backend = McpBackend(mock.mcp_url, None)
        await backend.discover()
        with pytest.raises(ActionError):
            await backend.call_tool("HassTurnOn", {"name": "gibt es nicht"})
        with pytest.raises(UnsupportedActionError):
            await backend.call_tool("Nope", {})


async def test_unreachable_mcp_marks_unavailable() -> None:
    backend = McpBackend("http://127.0.0.1:9/api/mcp", None)
    with pytest.raises(ActionError):
        await backend.discover()
    assert not backend.available


def test_mapping_rules() -> None:
    n = NAMES.get
    one = map_service_to_tool("light", "turn_on", None, {"entity_id": "light.wohnzimmer"}, n)
    assert one == [("HassTurnOn", {"name": "Wohnzimmer", "domain": ["light"]})]
    assert (
        map_service_to_tool(
            "light", "turn_on", {"brightness": 5}, {"entity_id": "light.wohnzimmer"}, n
        )
        == []
    )
    assert map_service_to_tool("lock", "lock", None, {"entity_id": "lock.x"}, n) == []
    assert map_service_to_tool("light", "turn_on", None, {"entity_id": "light.unbekannt"}, n) == []
    assert map_service_to_tool("light", "turn_on", None, {"area_id": "kueche"}, n) == []


async def test_fallback_to_ws_when_mcp_cannot_map() -> None:
    async with running_mock() as mock:
        store = StateStore()
        ha = HaWebSocket(mock.ws_url, "t", store, _noop)
        import asyncio

        task = asyncio.create_task(ha.run())
        try:
            await asyncio.wait_for(ha.synced.wait(), 5)
            mcp = McpBackend(mock.mcp_url, None, NAMES.get)
            await mcp.discover()
            disp = Dispatcher("mcp", mcp, WsBackend(ha))
            name, _ = await disp.call_service(
                "lock", "lock", None, {"entity_id": "lock.nuki_haustuer"}
            )
            assert name == "ws" and mock.ha.entities["lock.nuki_haustuer"]["state"] == "locked"
            name, _ = await disp.call_service(
                "light", "turn_on", None, {"entity_id": "light.wohnzimmer"}
            )
            assert name == "mcp"
        finally:
            task.cancel()
            store.close()


async def test_mcp_down_falls_back_to_ws_for_services() -> None:
    async with running_mock() as mock:
        store = StateStore()
        ha = HaWebSocket(mock.ws_url, "t", store, _noop)
        import asyncio

        task = asyncio.create_task(ha.run())
        try:
            await asyncio.wait_for(ha.synced.wait(), 5)
            dead = McpBackend("http://127.0.0.1:9/api/mcp", None, NAMES.get)
            disp = Dispatcher("mcp", dead, WsBackend(ha))
            name, _ = await disp.call_service(
                "light", "turn_on", None, {"entity_id": "light.wohnzimmer"}
            )
            assert name == "ws"
            with pytest.raises(UnsupportedActionError):  # ha_tool hat keinen Fallback
                await Dispatcher("mcp", None, WsBackend(ha)).call_tool("HassTurnOn", {})
        finally:
            task.cancel()
            store.close()


async def _noop(_: object) -> None:
    return None
