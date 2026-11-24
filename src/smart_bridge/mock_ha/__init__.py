"""Mock-HA für Dev-Profil und Tests: WebSocket-API und HA-MCP-Server mit ein paar Tools.

Start: `python -m smart_bridge.mock_ha` (Port `MOCK_HA_PORT`, Standard 8123).
WebSocket unter `/api/websocket`, MCP (Streamable HTTP) unter `/api/mcp`.
Kein Teil der Produktivlogik.
"""

from .server import MockHa, create_app

__all__ = ["MockHa", "create_app"]
