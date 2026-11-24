"""FastAPI-App. M1: Health-Endpunkte; Adapter-Checks kommen mit M2+."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response

from .adapters.ha_ws import HaWebSocket
from .adapters.mqtt import MqttAdapter, aiomqtt_factory
from .clock import utc_now
from .engine import AsyncioTimers, Outcome, RuleEngine
from .events import BridgeEvent
from .rules import Rule, duplicate_ids, load_dir
from .settings import Settings
from .state import StateStore

log = logging.getLogger(__name__)

Check = Callable[[], Awaitable[str]]  # "ok" | "degraded" | "down"


def _flag_check(flag: Callable[[], bool]) -> Check:
    async def check() -> str:
        return "ok" if flag() else "down"

    return check


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


def _load_rules(directory: Path) -> list[Rule]:
    results = load_dir(directory) if directory.is_dir() else []
    for err in duplicate_ids(results):
        log.error(err)
    seen: set[str] = set()
    rules: list[Rule] = []
    for res in results:
        for err in res.errors:
            log.error("%s: %s", res.path, err)  # letzte gültige Version: Hot-Reload folgt in M10
        for rule in res.rules:
            if rule.id not in seen:
                seen.add(rule.id)
                rules.append(rule)
    return rules


def app_factory() -> FastAPI:
    """Produktiv-App: StateStore und Adapter laufen als Hintergrund-Tasks."""
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(settings.data_dir / "bridge.db")

    async def outcome_sink(outcome: Outcome) -> None:
        log.info("outcome %s", outcome)  # AlertStore (M5) und Executor (M6) folgen

    timers = AsyncioTimers()
    engine = RuleEngine(store, outcome_sink, now=utc_now, timers=timers, zone=settings.zone)
    store.subscribe(engine.on_state_change)
    for rule_id, problem in engine.load(_load_rules(settings.config_dir / "rules")).items():
        log.error("rule %s rejected: %s", rule_id, problem)  # Fehler-Alert folgt mit M5

    async def sink(event: BridgeEvent) -> None:
        await engine.on_event(event)

    ha = (
        HaWebSocket(settings.ha_url, settings.ha_token, store, sink)
        if settings.ha_url and settings.ha_token
        else None
    )
    mqtt = (
        MqttAdapter(aiomqtt_factory(settings), sink, engine.index.mqtt_filters())
        if settings.mqtt_host
        else None
    )

    async def start_engine() -> None:
        if ha:
            await ha.synced.wait()  # erst nach der Vollsynchronisation
        await engine.start()

    checks: dict[str, Check] = {}
    if ha:
        checks["ha"] = _flag_check(lambda: ha.synced.is_set())
    if mqtt:
        checks["mqtt"] = _flag_check(lambda: mqtt.connected)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(a.run()) for a in (ha, mqtt) if a is not None]
        tasks.append(asyncio.create_task(start_engine()))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            timers.cancel_all()
            store.close()

    app = create_app(settings, checks)
    app.router.lifespan_context = lifespan

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        return {
            eid: {"state": st.state, "stale": st.stale, "last_changed": st.last_changed.isoformat()}
            for eid, st in store.all().items()
        }

    @app.post("/api/webhooks/{webhook_id}", status_code=202)
    async def webhook(webhook_id: str) -> dict[str, str]:
        if webhook_id not in engine.index.webhook:
            raise HTTPException(status_code=404)
        await engine.on_event(BridgeEvent("webhook", "webhook", {"webhook_id": webhook_id}))
        return {"status": "accepted"}

    return app
