"""Aktions-Backends: HA-MCP-Server (Assist-Intents) und HA-WebSocket (`call_service`)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..adapters.ha_ws import HaWebSocket
from .errors import ActionError, UnsupportedActionError

log = logging.getLogger(__name__)

# Der HA-MCP-Server stellt nur Assist-Intents für freigegebene Entities bereit.
# Abbildbar ist daher nur ein kleiner Teil der Services.
_SERVICE_TO_TOOL = {"turn_on": "HassTurnOn", "turn_off": "HassTurnOff"}


class ServiceBackend(Protocol):
    name: str

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> Any: ...

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any: ...


class WsBackend:
    name = "ws"

    def __init__(self, ha: HaWebSocket) -> None:
        self._ha = ha

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> Any:
        try:
            return await self._ha.call_service(domain, service, data, target)
        except Exception as exc:
            raise ActionError(f"ws call_service {domain}.{service}: {exc!r}") from exc

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        raise UnsupportedActionError("ha_tool needs the MCP backend")


def _entity_ids(target: dict[str, Any] | None, data: dict[str, Any] | None) -> list[str]:
    raw = (target or {}).get("entity_id") or (data or {}).get("entity_id")
    if raw is None:
        return []
    return [raw] if isinstance(raw, str) else list(raw)


def map_service_to_tool(
    domain: str,
    service: str,
    data: dict[str, Any] | None,
    target: dict[str, Any] | None,
    friendly_name: Callable[[str], str | None],
) -> list[tuple[str, dict[str, Any]]]:
    """Service → Assist-Intent-Aufrufe (je Entity einer). Leer, wenn nicht abbildbar."""
    tool = _SERVICE_TO_TOOL.get(service)
    extra = {k: v for k, v in (data or {}).items() if k != "entity_id"}
    entities = _entity_ids(target, data)
    if tool is None or extra or not entities or set(target or {}) - {"entity_id"}:
        return []
    calls: list[tuple[str, dict[str, Any]]] = []
    for entity_id in entities:
        name = friendly_name(entity_id)
        if not name or entity_id.split(".")[0] != domain and domain != "homeassistant":
            return []
        calls.append((tool, {"name": name, "domain": [entity_id.split(".")[0]]}))
    return calls


class McpBackend:
    """Streamable-HTTP-Client zum HA-MCP-Server (`HA_MCP_URL`, Bearer = HA-Token).

    Pro Aufruf eine kurze Sitzung: robust gegen HA-Neustarts, keine Zustände zu pflegen.
    """

    name = "mcp"

    def __init__(
        self,
        url: str,
        token: str | None,
        friendly_name: Callable[[str], str | None] = lambda _: None,
    ) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._friendly_name = friendly_name
        self.tools: set[str] | None = None  # None: Discovery noch nicht erfolgt
        self.available = False

    async def _with_session(self, fn: Callable[[ClientSession], Any]) -> Any:
        async with streamablehttp_client(self._url, headers=self._headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await fn(session)

    async def discover(self) -> set[str]:
        async def run(session: ClientSession) -> set[str]:
            return {t.name for t in (await session.list_tools()).tools}

        try:
            self.tools = await self._with_session(run)
        except Exception as exc:
            self.available = False
            raise ActionError(f"MCP tool discovery failed: {exc!r}") from exc
        self.available = True
        return self.tools or set()

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        if self.tools is not None and tool not in self.tools:
            raise UnsupportedActionError(f"MCP tool {tool!r} not available")

        async def run(session: ClientSession) -> Any:
            return await session.call_tool(tool, arguments)

        try:
            result = await self._with_session(run)
        except Exception as exc:
            self.available = False
            raise ActionError(f"MCP call {tool}: {exc!r}") from exc
        self.available = True
        if result.isError:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            raise ActionError(f"MCP tool {tool} failed: {text}")
        return result

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> Any:
        calls = map_service_to_tool(domain, service, data, target, self._friendly_name)
        if not calls:
            raise UnsupportedActionError(f"{domain}.{service} has no MCP equivalent")
        if self.tools is not None and any(t not in self.tools for t, _ in calls):
            raise UnsupportedActionError(f"{domain}.{service}: MCP tool not available")
        return [await self.call_tool(t, a) for t, a in calls]
