from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn

from smart_bridge.mock_ha import MockHa, create_app


@dataclass
class Running:
    ha: MockHa
    port: int

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/websocket"

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/mcp"


@asynccontextmanager
async def running_mock(token: str | None = None) -> AsyncIterator[Running]:
    ha = MockHa(token)
    config = uvicorn.Config(create_app(ha), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield Running(ha, port)
    finally:
        server.should_exit = True
        await task
