"""Backend-Auswahl (`executor: mcp | ws | auto`) mit Fallback auf WebSocket."""

from __future__ import annotations

import logging
from typing import Any

from .backends import ServiceBackend
from .errors import ActionError, UnsupportedActionError

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, mode: str, mcp: ServiceBackend | None, ws: ServiceBackend | None) -> None:
        self.mode, self._mcp, self._ws = mode, mcp, ws

    def _order(self) -> list[ServiceBackend]:
        # auto: Services exakt über WS, MCP nur wenn WS fehlt; mcp/ws: gewünschtes zuerst
        first, second = (
            (self._ws, self._mcp) if self.mode in ("ws", "auto") else (self._mcp, self._ws)
        )
        return [b for b in (first, second) if b is not None]

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> tuple[str, Any]:
        backends = self._order()
        if not backends:
            raise ActionError("no executor backend configured")
        if self.mode == "ws":
            backends = [b for b in backends if b.name == "ws"] or backends
        last: Exception | None = None
        for backend in backends:
            try:
                return backend.name, await backend.call_service(domain, service, data, target)
            except ActionError as exc:
                last = exc
                log.warning("%s failed for %s.%s: %s", backend.name, domain, service, exc)
        raise ActionError(str(last))

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> tuple[str, Any]:
        if self._mcp is None:
            raise UnsupportedActionError("ha_tool needs HA_MCP_URL")
        return self._mcp.name, await self._mcp.call_tool(tool, arguments)
