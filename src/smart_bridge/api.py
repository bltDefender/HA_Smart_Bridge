"""FastAPI-App. M1: Health-Endpunkte; Adapter-Checks kommen mit M2+."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Response

from .settings import Settings

Check = Callable[[], Awaitable[str]]  # "ok" | "degraded" | "down"


def create_app(settings: Settings | None = None, checks: dict[str, Check] | None = None) -> FastAPI:
    app = FastAPI(title="smart-bridge")
    cfg = settings or Settings.from_env()
    registered = checks or {}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, Any]:
        results: dict[str, str] = {}
        for name, check in registered.items():
            try:
                results[name] = await check()
            except Exception:  # Check darf /readyz nie crashen
                results[name] = "down"
        if any(v == "down" for k, v in results.items() if k != "qdrant"):
            status = "down"
            response.status_code = 503
        elif any(v != "ok" for v in results.values()):
            status = "degraded"  # z. B. Qdrant-Ausfall: nur degraded
        else:
            status = "ok"
        return {"status": status, "checks": results, "dry_run": cfg.dry_run}

    return app


def app_factory() -> FastAPI:
    return create_app()
