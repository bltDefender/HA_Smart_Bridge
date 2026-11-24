"""FastAPI-App. M1: Health-Endpunkte; Adapter-Checks kommen mit M2+."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response

from .adapters.ha_ws import HaWebSocket
from .adapters.mqtt import MqttAdapter, aiomqtt_factory
from .alerts import AlertPublisher, AlertStore, TargetCache
from .audit import AuditLog
from .catalog import (
    REGISTRY_EVENTS,
    Catalog,
    CatalogConfig,
    Descriptions,
    VectorIndex,
    fetch_registry,
    make_embedder,
)
from .clock import utc_now
from .engine import AsyncioTimers, Outcome, RuleEngine
from .engine.outcomes import ActionsRequested
from .events import BridgeEvent
from .executor import (
    ActionExecutor,
    Dispatcher,
    McpBackend,
    RunStore,
    WsBackend,
    check_tools,
)
from .history import StateHistory
from .mcp_server import ReadService, build_mcp, guarded_route
from .rules import Rule, duplicate_ids, load_dir
from .scheduler import AstralSun, FireTimeStore, Scheduler, workday_provider
from .scheduler.sun import sun_drift
from .settings import Settings
from .simulate import Env
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


def _load_rules(directory: Path, hashes: dict[str, str] | None = None) -> list[Rule]:
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
                if hashes is not None:
                    hashes[rule.id] = res.file_hash
                rules.append(rule)
    return rules


def app_factory() -> FastAPI:
    """Produktiv-App: StateStore und Adapter laufen als Hintergrund-Tasks."""
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(settings.data_dir / "bridge.db")

    alerts: AlertStore

    run_tasks: set[asyncio.Task[Any]] = set()

    async def outcome_sink(outcome: Outcome) -> None:
        if isinstance(outcome, ActionsRequested):
            # Aktionen laufen entkoppelt, damit Delays/wait_for die Engine nicht blockieren
            task = asyncio.create_task(executor.run(outcome))
            run_tasks.add(task)
            task.add_done_callback(run_tasks.discard)
            return
        await alerts.handle(outcome)

    timers = AsyncioTimers()
    sun = AstralSun(settings.latitude, settings.longitude, settings.zone)
    engine = RuleEngine(
        store,
        outcome_sink,
        now=utc_now,
        timers=timers,
        zone=settings.zone,
        sun=sun,
        is_workday=workday_provider(settings.holiday_country, settings.holiday_subdiv),
    )
    store.subscribe(engine.on_state_change)
    alerts = AlertStore(
        lambda: engine.rules, settings.data_dir / "bridge.db", utc_now, engine.templater.render
    )
    file_hashes: dict[str, str] = {}
    load_errors = engine.load(_load_rules(settings.config_dir / "rules", file_hashes))
    for rule_id, problem in load_errors.items():
        log.error("rule %s rejected: %s", rule_id, problem)

    fire_store = FireTimeStore(settings.data_dir / "bridge.db")
    scheduler = Scheduler(
        now=utc_now,
        timers=AsyncioTimers(),
        zone=settings.zone,
        store=fire_store,
        sun=sun,
        on_trigger=engine.on_time_trigger,
        on_window=engine.reevaluate,
    )
    for rule_id, problem in scheduler.load(list(engine.rules.values())).errors.items():
        log.error("rule %s has unschedulable triggers: %s", rule_id, problem)
        load_errors[rule_id] = problem

    targets = TargetCache()

    catalog: Catalog | None = None

    async def sink(event: BridgeEvent) -> None:
        if catalog is not None and event.type in REGISTRY_EVENTS:
            catalog.request_sync()
        if targets.on_event(event):
            if catalog is not None:
                catalog.request_sync()
            warnings = targets.validate(engine.rules.values(), lambda e: store.get(e) is not None)
            for rule_id, warning in warnings.items():
                log.warning("rule %s: %s", rule_id, warning)
            return
        await engine.on_event(event)

    ha = (
        HaWebSocket(settings.ha_url, settings.ha_token, store, sink)
        if settings.ha_url and settings.ha_token
        else None
    )
    mqtt: MqttAdapter | None = None
    publisher: AlertPublisher | None = None
    if settings.mqtt_host:

        async def mqtt_publish(topic: str, payload: str, retain: bool) -> None:
            assert mqtt is not None
            await mqtt.publish(topic, payload, retain)

        publisher = AlertPublisher(alerts, mqtt_publish, settings.zone)
        alerts.subscribe(publisher.publish_state)
        mqtt = MqttAdapter(
            aiomqtt_factory(settings),
            sink,
            engine.index.mqtt_filters(),
            on_connect=publisher.on_connect,
        )
    if ha:
        targets = TargetCache(ha.fire_event)

    if settings.qdrant_url:
        from qdrant_client import AsyncQdrantClient

        qdrant = AsyncQdrantClient(location=settings.qdrant_url)
        cache = settings.embed_cache_dir or settings.data_dir / "embed-cache"
        catalog = Catalog(
            VectorIndex(qdrant, make_embedder(settings.embed_model, cache)),
            store,
            lambda: engine.rules,
            lambda: targets,
            registry=(lambda: fetch_registry(ha.command)) if ha else None,
            config=CatalogConfig.load(settings.config_dir / "catalog.yaml"),
            descriptions=Descriptions(settings.config_dir / "descriptions"),
            file_hashes=lambda: file_hashes,
        )

    history = StateHistory(
        settings.data_dir / "bridge.db",
        lambda e: e in engine.index.entity or (catalog is not None and e in catalog.selected),
        settings.state_history_days,
    )
    store.subscribe(history.on_change)

    audit = AuditLog(settings.data_dir / "bridge.db", utc_now)
    mcp_server = None
    if settings.mcp_server_enabled:
        if not settings.mcp_server_token:
            raise RuntimeError("MCP_SERVER_ENABLED requires MCP_SERVER_TOKEN")
        read_service = ReadService(
            engine,
            store,
            alerts,
            lambda: targets,
            history,
            catalog,
            Env(
                settings.zone,
                sun,
                workday_provider(settings.holiday_country, settings.holiday_subdiv),
            ),
            utc_now,
            settings.config_dir,
            settings.state_history_days,
        )
        mcp_server = build_mcp(read_service, audit)

    def friendly_name(entity_id: str) -> str | None:
        state = store.get(entity_id)
        name = state.attributes.get("friendly_name") if state else None
        return name if isinstance(name, str) else None

    mcp_backend = (
        McpBackend(settings.ha_mcp_url, settings.ha_token, friendly_name)
        if settings.ha_mcp_url
        else None
    )
    run_store = RunStore(settings.data_dir / "bridge.db")

    async def http_request(method: str, url: str, headers: dict[str, str], body: Any) -> int:
        async with httpx.AsyncClient(timeout=30) as client:
            kwargs = {"json": body} if not isinstance(body, str | None) else {"content": body}
            return (await client.request(method, url, headers=headers, **kwargs)).status_code

    executor = ActionExecutor(
        Dispatcher(settings.executor, mcp_backend, WsBackend(ha) if ha else None),
        run_store,
        lambda: engine.rules,
        engine.eval_condition,
        mqtt_publish=mqtt.publish if mqtt else None,
        http=http_request,
        dry_run=settings.dry_run,
        templater=engine.templater,
    )

    async def on_state(change: Any) -> None:
        executor.notify_state_changed()

    store.subscribe(on_state)

    async def discover_tools() -> None:
        if mcp_backend is None:
            return
        try:
            tools = await mcp_backend.discover()
        except Exception as exc:  # MCP-Ausfall verhindert den Start nicht; Fallback WS
            log.warning("HA MCP tool discovery failed: %s", exc)
            return
        for rule_id, problem in check_tools(list(engine.rules.values()), tools).items():
            log.error("rule %s: %s", rule_id, problem)
            load_errors[rule_id] = problem

    def _check_sun_drift() -> None:
        """Warnung, wenn `sun.sun` in HA und die lokale astral-Berechnung auseinanderliegen."""
        state = store.get("sun.sun")
        raw = state.attributes.get("next_setting") if state else None
        if not isinstance(raw, str):
            return
        drift = sun_drift(raw, sun, settings.zone, timedelta(minutes=5))
        if drift is not None:
            log.warning("sun.sun differs from local sun calculation by %s", drift)

    async def start_engine() -> None:
        if ha:
            await ha.synced.wait()  # erst nach der Vollsynchronisation
        await discover_tools()
        await engine.start()
        await alerts.reconcile({r for r in engine.rules if engine.alert_state(r).active})
        await alerts.replace_system(load_errors)
        await scheduler.start()
        await targets.request()
        if catalog is not None:
            catalog.request_sync()
        _check_sun_drift()

    checks: dict[str, Check] = {}
    if ha:
        checks["ha"] = _flag_check(lambda: ha.synced.is_set())
    if mqtt:
        checks["mqtt"] = _flag_check(lambda: mqtt.connected)
    if mcp_backend:
        checks["ha_mcp"] = _flag_check(lambda: mcp_backend.available)

    if catalog is not None:
        checks["qdrant"] = catalog.check

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(a.run()) for a in (ha, mqtt) if a is not None]
        tasks.append(asyncio.create_task(start_engine()))
        if catalog is not None:
            tasks.append(asyncio.create_task(catalog.run()))
        try:
            async with AsyncExitStack() as stack:
                if mcp_server is not None:
                    await stack.enter_async_context(mcp_server.session_manager.run())
                yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for t in list(run_tasks):
                t.cancel()
            await asyncio.gather(*run_tasks, return_exceptions=True)
            run_store.close()
            history.close()
            audit.close()
            timers.cancel_all()
            scheduler.stop()
            fire_store.close()
            alerts.close()
            store.close()

    app = create_app(settings, checks)
    app.router.lifespan_context = lifespan
    if mcp_server is not None and settings.mcp_server_token:
        app.router.routes.append(guarded_route(mcp_server, settings.mcp_server_token))

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        return {
            eid: {"state": st.state, "stale": st.stale, "last_changed": st.last_changed.isoformat()}
            for eid, st in store.all().items()
        }

    @app.get("/api/alerts")
    async def api_alerts() -> dict[str, Any]:
        return {"alerts": [x.to_json(settings.zone) for x in alerts.alerts()]}

    @app.get("/api/pause")
    async def api_pause_state() -> dict[str, bool]:
        return {"paused": executor.paused}

    @app.post("/api/pause")
    async def api_pause() -> dict[str, bool]:
        executor.set_paused(True)
        log.warning("kill switch: actions paused")
        return {"paused": True}

    @app.post("/api/resume")
    async def api_resume() -> dict[str, bool]:
        executor.set_paused(False)
        log.warning("kill switch released: actions resumed")
        return {"paused": False}

    @app.get("/api/runs")
    async def api_runs(rule_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        return {"runs": run_store.recent(rule_id, min(max(limit, 1), 200))}

    @app.post("/api/webhooks/{webhook_id}", status_code=202)
    async def webhook(webhook_id: str) -> dict[str, str]:
        if webhook_id not in engine.index.webhook:
            raise HTTPException(status_code=404)
        await engine.on_event(BridgeEvent("webhook", "webhook", {"webhook_id": webhook_id}))
        return {"status": "accepted"}

    return app
