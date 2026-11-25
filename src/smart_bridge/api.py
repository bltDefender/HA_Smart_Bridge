"""FastAPI-App. M1: Health-Endpunkte; Adapter-Checks kommen mit M2+."""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse

from .adapters.ha_ws import HaWebSocket
from .adapters.mqtt import MqttAdapter, aiomqtt_factory
from .alerts import AlertPublisher, AlertStore, TargetCache
from .approvals import ApprovalError, ApprovalStore, load_secret
from .audit import AuditLog
from .authoring import CircuitBreaker, Pipeline, RuleManager, RuleRepository
from .authoring.repository import RepositoryError
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
from .engine.outcomes import ActionsRequested, AlertActivated
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
from .mcp_server import ReadService, WriteService, build_mcp, guarded_route
from .policy import Policy
from .rules import Rule, duplicate_ids, load_dir
from .scheduler import AstralSun, FireTimeStore, Scheduler, workday_provider
from .scheduler.sun import sun_drift
from .settings import Settings
from .simulate import Env, backtest_rule
from .state import StateStore
from .ui import UiReader, build_ui_router, rule_to_yaml
from .ui.static import mount_ui, resolve_dist

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
    policy = Policy.load(settings.config_dir / "policy.yaml")

    alerts: AlertStore
    manager: RuleManager

    run_tasks: set[asyncio.Task[Any]] = set()

    def spawn(coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        run_tasks.add(task)
        task.add_done_callback(run_tasks.discard)

    def counted(rule_id: str) -> bool:
        """Circuit-Breaker: `True`, wenn die Regel gestoppt wird (per_entity: je Original)."""
        rule = engine.rules.get(rule_id)
        key = (rule.parent_id if rule else None) or rule_id
        if breaker.record(key):
            spawn(manager.trip(key))
            return True
        return False

    async def outcome_sink(outcome: Outcome) -> None:
        if isinstance(outcome, ActionsRequested):
            if outcome.shadow:
                await shadow_alert(outcome)
            elif counted(outcome.rule_id):
                return
            # Aktionen laufen entkoppelt, damit Delays/wait_for die Engine nicht blockieren
            spawn(executor.run(outcome))
            return
        if isinstance(outcome, AlertActivated) and counted(outcome.rule_id):
            return
        await alerts.handle(outcome)

    shadow_keys: set[str] = set()

    async def shadow_alert(outcome: ActionsRequested) -> None:
        rule = engine.rules.get(outcome.rule_id)

        def label(a: Any) -> str:
            extra = a.model_extra or {}
            return f"{a.type} {extra.get('service') or extra.get('tool') or ''}".strip()

        names = ", ".join(label(a) for a in outcome.actions)
        shadow_keys.add(outcome.rule_id)
        await alerts.set_system(
            f"shadow_{outcome.rule_id}",
            f"Schattenregel hätte ausgelöst: {(rule.title if rule else None) or outcome.rule_id}",
            f"Aktionen nur geloggt, nicht ausgeführt: {names}",
            "info",
        )

    breaker = CircuitBreaker(
        settings.data_dir / "bridge.db", utc_now, policy.max_activations_per_hour
    )

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
    # Vorab-Index (z. B. MQTT-Filter); die endgültigen Regeln setzt `manager.startup()`.
    # Die Engine ist bis `engine.start()` inaktiv, daher feuert nichts vor Stufenprüfung/Freigabe.
    load_errors = engine.load(_load_rules(settings.config_dir / "rules"))

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
            file_hashes=lambda: manager.file_hashes,
        )

    history = StateHistory(
        settings.data_dir / "bridge.db",
        lambda e: e in engine.index.entity or (catalog is not None and e in catalog.selected),
        settings.state_history_days,
    )
    store.subscribe(history.on_change)

    audit = AuditLog(settings.data_dir / "bridge.db", utc_now)
    env = Env(
        settings.zone, sun, workday_provider(settings.holiday_country, settings.holiday_subdiv)
    )
    approvals = ApprovalStore(
        settings.config_dir / "approvals",
        settings.data_dir / "bridge.db",
        load_secret(settings.approval_secret, settings.data_dir),
        audit,
        utc_now,
        timedelta(hours=settings.approval_ttl_hours),
    )

    def entity_exists(entity_id: str) -> bool:
        return store.get(entity_id) is not None or (
            catalog is not None and entity_id in catalog.selected
        )

    async def suggest(entity_id: str) -> list[str]:
        if catalog is None:
            return []
        hits = await catalog.search_entities(
            entity_id.replace("_", " ").replace(".", " "), None, None, 3
        )
        return [str(h["entity_id"]) for h in hits if "entity_id" in h]

    async def pipeline_backtest(text: str, days: int) -> dict[str, Any]:
        return await backtest_rule(text, env, history, utc_now(), days)

    pipeline = Pipeline(
        policy,
        entity_exists=entity_exists,
        suggest=suggest if catalog is not None else None,
        targets=lambda: targets,
        search_rules=(lambda q: catalog.search_rules(q, 3)) if catalog is not None else None,
        backtest=pipeline_backtest,
    )
    engine_ready = False
    known_tools: set[str] | None = None
    rule_error_keys: set[str] = set()

    async def apply_rules(rules: list[Rule], errors: dict[str, str]) -> None:
        """Hot-Reload: Engine, Scheduler, Alerts, MQTT-Filter und Katalog aktualisieren."""
        problems = dict(errors)
        problems.update(engine.load(rules))
        problems.update(scheduler.load(list(engine.rules.values())).errors)
        if known_tools is not None:
            problems.update(check_tools(list(engine.rules.values()), known_tools))
        load_errors.clear()
        load_errors.update(problems)
        for rule_id, problem in problems.items():
            log.error("rule %s rejected: %s", rule_id, problem)
        status = {r.id: r.status for r in engine.rules.values()}
        for rid in [k for k in shadow_keys if status.get(k) != "shadow"]:
            shadow_keys.discard(rid)
            await alerts.clear_system(f"shadow_{rid}")
        if not engine_ready:
            return
        await engine.evaluate_all()
        await alerts.retain(set(engine.rules))
        for key in rule_error_keys - set(problems):
            await alerts.clear_system(key)
        for rule_id, problem in problems.items():
            await alerts.set_system(rule_id, f"Regel {rule_id} abgelehnt", problem)
        rule_error_keys.clear()
        rule_error_keys.update(problems)
        if mqtt:
            await mqtt.set_topics(engine.index.mqtt_filters())
        if catalog is not None:
            catalog.request_sync()

    async def notify_pending(rule: Rule) -> None:
        if not (ha and settings.approval_notify_service and settings.bridge_public_url):
            return
        tokens = approvals.issue(rule)
        domain, _, service = settings.approval_notify_service.partition(".")
        base = f"{settings.bridge_public_url}/api/approvals"
        await ha.call_service(
            domain,
            service,
            {
                "title": f"Freigabe nötig: {rule.title or rule.id}",
                "message": f"Stufe-2-Regel {rule.id} läuft im Schattenmodus. "
                f"{rule.intent or ''}".strip(),
                "data": {
                    "actions": [
                        {
                            "action": "URI",
                            "title": "Freigeben",
                            "uri": f"{base}/{tokens['approved']}",
                        },
                        {
                            "action": "URI",
                            "title": "Ablehnen",
                            "uri": f"{base}/{tokens['rejected']}",
                        },
                    ]
                },
            },
        )

    manager = RuleManager(
        RuleRepository(settings.config_dir / "rules"),
        pipeline,
        approvals,
        breaker,
        audit,
        apply_rules,
        lambda key, title, message, severity: alerts.set_system(key, title, message, severity),
        alerts.clear_system,
        notify_pending,
    )
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
            env,
            utc_now,
            settings.config_dir,
            settings.state_history_days,
            versions=lambda rid: [v.summary() for v in manager.repo.versions(rid)],
        )
        writes = WriteService(manager, alerts, catalog, utc_now, policy.writes_per_hour)
        mcp_server = build_mcp(read_service, audit, writes)

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
        nonlocal known_tools
        known_tools = tools
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
        nonlocal engine_ready
        await manager.startup()
        if ha:
            await ha.synced.wait()  # erst nach der Vollsynchronisation
        await discover_tools()
        await engine.start()
        await alerts.reconcile({r for r in engine.rules if engine.alert_state(r).active})
        for rule_id, problem in load_errors.items():
            await alerts.set_system(rule_id, f"Regel {rule_id} abgelehnt", problem)
        rule_error_keys.update(load_errors)
        await scheduler.start()
        await targets.request()
        if catalog is not None:
            catalog.request_sync()
        _check_sun_drift()
        engine_ready = True
        await manager.run(settings.rule_scan_seconds)  # Hot-Reload-Watcher läuft bis zum Shutdown

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
            approvals.close()
            breaker.close()
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

    def ui_entity(entity_id: str) -> dict[str, Any] | None:
        st = store.get(entity_id)
        if st is None:
            return None
        return {
            "name": friendly_name(entity_id) or entity_id,
            "area": catalog.area_of(entity_id) if catalog else None,
            "state": st.state,
            "stale": st.stale,
        }

    def ui_owner(run_rule_id: str) -> str:
        rule = engine.rules.get(run_rule_id)
        return (rule.parent_id if rule and rule.parent_id else None) or run_rule_id

    def ui_alert_active(rule_id: str) -> bool:
        return any(
            engine.alert_state(r.id).active
            for r in engine.rules.values()
            if r.id == rule_id or r.parent_id == rule_id
        )

    def ui_version_text(rule_id: str, version: int) -> str:
        try:
            return manager.repo.version_text(rule_id, version)
        except RepositoryError as exc:
            raise LookupError(str(exc)) from exc

    async def ui_search(query: str, limit: int) -> list[str]:
        assert catalog is not None
        return [str(h["rule_id"]) for h in await catalog.search_rules(query, limit)]

    # Der UI-Router bekommt nur lesende Funktionen, nie Executor, Manager oder MCP-Schreibtools.
    ui_reader = UiReader(
        rules=lambda: dict(manager.final),
        tier=policy.tier_of,
        entity=ui_entity,
        owner=ui_owner,
        alert_active=ui_alert_active,
        runs=run_store.recent,
        run_counts=run_store.counts,
        versions=lambda rid: [v.summary() for v in manager.repo.versions(rid)],
        version_text=ui_version_text,
        rule_yaml=lambda rid: rule_to_yaml(manager.final[rid]),
        states=lambda: {
            eid: {"state": st.state, "stale": st.stale} for eid, st in store.all().items()
        },
        alerts=lambda: [x.to_json(settings.zone) for x in alerts.alerts()],
        now=utc_now,
        search=ui_search if catalog else None,
    )
    app.include_router(build_ui_router(ui_reader, settings.ui_token))
    mount_ui(app, resolve_dist(settings.ui_dist_dir), settings.ui_frame_ancestors)

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

    @app.get("/api/approvals/{token}", response_class=HTMLResponse)
    async def approval_page(token: str) -> str:
        """Bestätigungsseite (ändert nichts, damit Link-Vorschauen keine Freigabe auslösen)."""
        try:
            info = approvals.inspect(token)
        except ApprovalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rule = manager.raw(info["rule_id"])
        title = html.escape((rule.title if rule else None) or info["rule_id"])
        verb = "Freigeben" if info["decision"] == "approved" else "Ablehnen"
        return (
            "<!doctype html><meta charset=utf-8><title>Regel-Freigabe</title>"
            f"<h1>{title}</h1><p>{html.escape((rule.intent if rule else '') or '')}</p>"
            f'<form method="post"><button type="submit">{verb}</button></form>'
        )

    @app.post("/api/approvals/{token}")
    async def approval_redeem(token: str) -> dict[str, str]:
        """Signierter Einmal-Webhook (aus der HA-Benachrichtigung). Schreibt Freigabe + Audit."""
        try:
            info = approvals.inspect(token)
            decision = approvals.redeem(token, manager.raw(info["rule_id"]), "webhook")
        except ApprovalError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        await manager.scan()
        return {"rule_id": info["rule_id"], "decision": decision}

    return app
