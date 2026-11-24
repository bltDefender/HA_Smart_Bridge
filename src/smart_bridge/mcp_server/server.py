"""FastMCP-Server (Streamable HTTP unter `/mcp`, Bearer-Token). M9: nur lesende Tools.

Jeder Aufruf landet im Audit-Log (SQLite + JSON-Log). Schreibende Tools folgen in M10 und laufen
durch dieselbe Validierungs-Pipeline wie Dateiänderungen.
"""

from __future__ import annotations

import functools
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ..audit import AuditLog
from .examples import AUTHOR_RULE_PROMPT
from .service import ReadService, ToolError

ACTOR = "claude-mcp"
MCP_PATH = "/mcp"


class BearerAuth:
    """ASGI-Wrapper: ohne gültiges Token kein Zugriff (konstante Vergleichszeit)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app, self._token = app, token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            header = Request(scope).headers.get("authorization", "")
            scheme, _, supplied = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                supplied.strip().encode(), self._token
            ):
                response = JSONResponse(
                    {"error": "unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def build_mcp(service: ReadService, audit: AuditLog) -> FastMCP:
    mcp = FastMCP(
        "smart-bridge",
        instructions=(
            "Regeln der Smart-Bridge verstehen und prüfen. Nutze den Prompt `author_rule`. "
            "Entity-IDs immer über `search_entities` finden."
        ),
        streamable_http_path=MCP_PATH,
        # Das Bearer-Token schützt den Zugriff; im LAN gibt es beliebige Hostnamen
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def audited(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            details = {k: v for k, v in kwargs.items()}
            try:
                result = await fn(*args, **kwargs)
            except ToolError as exc:
                audit.record(ACTOR, f"mcp.{fn.__name__}", None, details, f"error: {exc}")
                raise ValueError(str(exc)) from exc
            except Exception as exc:
                audit.record(
                    ACTOR, f"mcp.{fn.__name__}", None, details, f"error: {type(exc).__name__}"
                )
                raise
            audit.record(ACTOR, f"mcp.{fn.__name__}", None, details)
            return result

        return wrapper

    @mcp.tool()
    @audited
    async def search_entities(
        query: str, domain: str | None = None, area: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Semantische Suche im Entity-Katalog (deutsch). Liefert entity_id, area, Beschreibung."""
        return await service.search_entities(query, domain, area, limit)

    @mcp.tool()
    @audited
    async def get_state(entity_id: str) -> dict[str, Any]:
        """Aktueller Zustand einer Entity (inkl. `stale`)."""
        return service.get_state(entity_id)

    @mcp.tool()
    @audited
    async def list_alerts() -> list[dict[str, Any]]:
        """Aktive Alerts wie im 3D-Hausmodell."""
        return service.list_alerts()

    @mcp.tool()
    @audited
    async def list_rules() -> list[dict[str, Any]]:
        """Alle geladenen Regeln mit Status, Ziel und aktuellem Alert-Zustand."""
        return service.list_rules()

    @mcp.tool()
    @audited
    async def search_rules(query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Semantische Suche über bestehende Regeln (Dubletten vor dem Anlegen prüfen)."""
        return await service.search_rules(query, limit)

    @mcp.tool()
    @audited
    async def list_model_targets() -> dict[str, Any]:
        """Ziele des 3D-Hausmodells (`haus3d_targets`) für `alert.target`."""
        return service.list_model_targets()

    @mcp.tool()
    @audited
    async def explain_rule(rule_id: str) -> dict[str, Any]:
        """Regel in Klartext mit `intent` und aktueller Bewertung je Bedingungsknoten."""
        return service.explain_rule(rule_id)

    @mcp.tool()
    @audited
    async def simulate_rule(rule_yaml: str, at: str | None = None) -> dict[str, Any]:
        """Dry-Run: Regel-YAML gegen aktuellen (oder per `at`, ISO-8601, historischen) Zustand."""
        return await service.simulate_rule(rule_yaml, at)

    @mcp.tool()
    @audited
    async def backtest_rule(rule_yaml: str, days: int = 7) -> dict[str, Any]:
        """Spielt `state_history` durch: Aktivierungen, Dauer, Aktionen, die gefeuert hätten."""
        return await service.backtest_rule(rule_yaml, days)

    @mcp.tool()
    @audited
    async def list_rule_versions(rule_id: str) -> list[dict[str, Any]]:
        """Versionen einer Regel (Versionierung folgt mit M10, bis dahin leer)."""
        return service.list_rule_versions(rule_id)

    # --- Resources ---------------------------------------------------------------

    def resource(name: str, body: Callable[[], str]) -> str:
        audit.record(ACTOR, f"mcp.resource.{name}")
        return body()

    @mcp.resource("bridge://docs/rule-schema", mime_type="application/json")
    def rule_schema() -> str:
        return resource("rule-schema", service.schema_json)

    @mcp.resource("bridge://docs/rule-examples", mime_type="application/yaml")
    def rule_examples() -> str:
        return resource("rule-examples", service.examples)

    @mcp.resource("bridge://policy", mime_type="application/yaml")
    def policy() -> str:
        """Read-only; die Policy ist für Claude nicht beschreibbar."""
        return resource("policy", service.policy_text)

    @mcp.resource("bridge://rules/{rule_id}", mime_type="application/yaml")
    def rule_resource(rule_id: str) -> str:
        return resource("rules", lambda: service.rule_yaml(rule_id))

    @mcp.resource("bridge://entities/{entity_id}", mime_type="application/json")
    def entity_resource(entity_id: str) -> str:
        return resource("entities", lambda: _json(service.entity_resource(entity_id)))

    @mcp.resource("bridge://alerts", mime_type="application/json")
    def alerts_resource() -> str:
        return resource("alerts", lambda: _json(service.list_alerts()))

    @mcp.prompt()
    def author_rule(request: str) -> str:
        """Fester Ablauf zum Anlegen einer Regel aus einem Nutzerwunsch."""
        audit.record(ACTOR, "mcp.prompt.author_rule")
        return AUTHOR_RULE_PROMPT.format(request=request)

    return mcp


def guarded_route(mcp: FastMCP, token: str) -> Route:
    """`/mcp`-Route der FastMCP-App, hinter dem Bearer-Token."""
    (route,) = (r for r in mcp.streamable_http_app().routes if isinstance(r, Route))
    return Route(MCP_PATH, BearerAuth(route.endpoint, token))
