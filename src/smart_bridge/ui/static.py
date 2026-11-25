"""Statische UI-Assets unter `/ui` (nur `GET`/`HEAD`) und Sicherheits-Header.

Das Frontend ist ein reiner Build (Svelte + Vite), ohne CDN. Die Auslieferung kennt keine
Schreibpfade; `StaticFiles` antwortet auf andere Methoden mit 405.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..settings import parse_frame_ancestors

UI_MOUNT = "/ui"


def csp(frame_ancestors: str) -> str:
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
        f"frame-ancestors {frame_ancestors}"
    )


class UiHeaders:
    """ASGI-Middleware: CSP (inkl. `frame-ancestors`) für alle `/ui`-Antworten."""

    def __init__(self, app: ASGIApp, frame_ancestors: str) -> None:
        self.app = app
        self.policy = csp(parse_frame_ancestors(frame_ancestors)).encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == UI_MOUNT or path.startswith(UI_MOUNT + "/")):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"content-security-policy"
                ]
                headers += [
                    (b"content-security-policy", self.policy),
                    (b"x-content-type-options", b"nosniff"),
                ]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def resolve_dist(configured: Path | None) -> Path | None:
    """Verzeichnis mit `index.html` des UI-Builds, sonst `None` (UI-API läuft trotzdem)."""
    candidate = configured if configured is not None else Path("ui/dist")
    return candidate if (candidate / "index.html").is_file() else None


def mount_ui(app: FastAPI, dist: Path | None, frame_ancestors: str) -> bool:
    """Bindet Header und Assets ein. Nach `include_router(build_ui_router(...))` aufrufen."""
    app.add_middleware(UiHeaders, frame_ancestors=frame_ancestors)
    if dist is None:
        return False
    app.mount(UI_MOUNT, StaticFiles(directory=dist, html=True), name="ui")
    return True
