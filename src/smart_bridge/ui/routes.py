"""Read-only UI-API unter `/ui/api`. Nur `GET`; kein Zugriff auf Executor, Manager oder MCP.

Der Router bekommt ausschließlich lesende Funktionen (`UiReader`).
"""

from __future__ import annotations

import asyncio
import difflib
import hmac
import json
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..catalog.documents import _action_entities, rule_entities
from ..clock import Now
from ..rules.models import Rule
from .graph import EntityLookup, build_graph

PREFIX = "/ui/api"
MAX_RUNS = 200
HEATMAP_DAYS = 14


@dataclass
class UiReader:
    rules: Callable[[], dict[str, Rule]]  # finalisiert, ohne per_entity-Expansion
    tier: Callable[[Rule], int]
    entity: EntityLookup
    owner: Callable[[str], str]  # Lauf-/Engine-ID → Regel-ID
    alert_active: Callable[[str], bool]
    runs: Callable[[str | None, int], list[dict[str, Any]]]
    run_counts: Callable[[datetime], list[tuple[str, str, int]]]
    versions: Callable[[str], list[dict[str, Any]]]
    version_text: Callable[[str, int], str]
    rule_yaml: Callable[[str], str]
    states: Callable[[], dict[str, dict[str, Any]]]
    alerts: Callable[[], list[dict[str, Any]]]
    now: Now
    search: Callable[[str, int], Awaitable[list[str]]] | None = None


def _status(rule: Rule) -> str:
    return "disabled" if not rule.enabled else rule.status


def _roles(rule: Rule) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {
        "trigger": set(),
        "condition": set(),
        "action": set(),
        "alert": set(),
    }
    for trig in rule.triggers or []:
        if isinstance(eid := getattr(trig, "entity_id", None), str):
            roles["trigger"].add(eid)
    if rule.condition:
        roles["condition"] = rule.condition.entity_ids(rule.bindings)
        if not rule.triggers:
            roles["trigger"] = set(roles["condition"])
    for action in [*(rule.actions or []), *(rule.on_activate or []), *(rule.on_clear or [])]:
        _action_entities(action.model_dump(by_alias=True), roles["action"])
    if rule.alert and "." in rule.alert.target and "{{" not in rule.alert.target:
        roles["alert"].add(rule.alert.target)
    return {k: {e for e in v if "{{" not in e} for k, v in roles.items()}


def _last_fired(reader: UiReader) -> dict[str, str]:
    last: dict[str, str] = {}
    for run in reader.runs(None, MAX_RUNS):  # neueste zuerst
        last.setdefault(reader.owner(run["rule_id"]), run["started_at"])
    return last


def _summary(
    reader: UiReader, rule: Rule, last: dict[str, str], areas: list[str]
) -> dict[str, Any]:
    return {
        "id": rule.id,
        "title": rule.title,
        "intent": rule.intent,
        "status": _status(rule),
        "tier": reader.tier(rule),
        "tags": rule.tags,
        "created_by": rule.created_by,
        "version": rule.version,
        "target": rule.alert.target if rule.alert else None,
        "severity": rule.alert.severity if rule.alert else None,
        "areas": areas,
        "has_actions": bool(rule.actions or rule.on_activate or rule.on_clear),
        "alert_active": reader.alert_active(rule.id),
        "last_fired": last.get(rule.id),
    }


def _areas(reader: UiReader, rule: Rule) -> list[str]:
    found = {(reader.entity(e) or {}).get("area") for e in rule_entities(rule)}
    return sorted(a for a in found if a)


async def event_stream(
    reader: UiReader, interval: float = 1.0, limit: int | None = None
) -> AsyncIterator[str]:
    """SSE: Zustände, Läufe und Alerts als Änderungen gegenüber dem letzten Stand."""

    def frame(event: str, data: Any) -> str:
        return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    seen_states: dict[str, Any] = {}
    seen_runs: set[str] = {r["run_id"] for r in reader.runs(None, 50)}
    seen_alerts: str | None = None
    yield frame("hello", {"at": reader.now().isoformat()})
    ticks = 0
    while limit is None or ticks < limit:
        ticks += 1
        used = {e for r in reader.rules().values() for e in rule_entities(r)}
        current = {e: s for e, s in reader.states().items() if e in used}
        for eid, st in current.items():
            if seen_states.get(eid) != st:
                seen_states[eid] = st
                yield frame("state", {"entity_id": eid, **st})
        for run in reversed(reader.runs(None, 50)):
            if run["run_id"] not in seen_runs:
                seen_runs.add(run["run_id"])
                yield frame(
                    "run",
                    {k: run[k] for k in ("run_id", "rule_id", "phase", "result", "started_at")}
                    | {"rule": reader.owner(run["rule_id"])},
                )
        alerts = json.dumps(reader.alerts(), default=str, sort_keys=True)
        if alerts != seen_alerts:
            seen_alerts = alerts
            yield frame("alerts", json.loads(alerts))
        await asyncio.sleep(interval)


def build_ui_router(reader: UiReader, token: str | None = None) -> APIRouter:
    def guard(request: Request, token_q: str | None = Query(default=None, alias="token")) -> None:
        if token is None:
            return
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else (token_q or "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    router = APIRouter(prefix=PREFIX, dependencies=[Depends(guard)])

    def rule_of(rule_id: str) -> Rule:
        rule = reader.rules().get(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"unknown rule {rule_id!r}")
        return rule

    @router.get("/rules")
    async def list_rules(
        q: str | None = None,
        area: str | None = None,
        tag: str | None = None,
        tier: int | None = Query(default=None, ge=0, le=2),
        status: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        last = _last_fired(reader)
        ranking: dict[str, int] | None = None
        if q and reader.search is not None:
            try:
                ranking = {rid: i for i, rid in enumerate(await reader.search(q, 50))}
            except Exception:  # Katalog degraded: auf Textsuche zurückfallen
                ranking = None
        rows = []
        for rule in reader.rules().values():
            row = _summary(reader, rule, last, _areas(reader, rule))
            if area and area not in row["areas"]:
                continue
            if tag and tag not in rule.tags:
                continue
            if tier is not None and row["tier"] != tier:
                continue
            if status and row["status"] != status:
                continue
            if created_by and rule.created_by != created_by:
                continue
            if q:
                text = f"{rule.id} {rule.title or ''} {rule.intent or ''}".lower()
                if ranking is not None:
                    if rule.id not in ranking:
                        continue
                elif q.lower() not in text:
                    continue
            rows.append(row)
        if ranking is not None:
            rows.sort(key=lambda r: ranking[r["id"]])
        else:
            rows.sort(key=lambda r: r["id"])
        return {"rules": rows, "semantic": ranking is not None}

    @router.get("/rules/{rule_id}")
    async def rule_detail(rule_id: str) -> dict[str, Any]:
        rule = rule_of(rule_id)
        return {
            **_summary(reader, rule, _last_fired(reader), _areas(reader, rule)),
            "yaml": reader.rule_yaml(rule_id),
            "versions": reader.versions(rule_id),
            "entities": {k: sorted(v) for k, v in _roles(rule).items()},
        }

    @router.get("/rules/{rule_id}/graph")
    async def rule_graph(rule_id: str, run_id: str | None = None) -> dict[str, Any]:
        rule = rule_of(rule_id)
        run = None
        if run_id:
            run = next((r for r in reader.runs(rule_id, MAX_RUNS) if r["run_id"] == run_id), None)
            if run is None:
                raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
        graph = build_graph(rule, reader.entity, run)
        graph |= {
            "status": _status(rule),
            "tier": reader.tier(rule),
            "alert_active": reader.alert_active(rule_id),
        }
        return graph

    @router.get("/rules/{rule_id}/runs")
    async def rule_runs(rule_id: str, limit: int = Query(default=20, ge=1, le=MAX_RUNS)) -> Any:
        rule_of(rule_id)
        return {"runs": reader.runs(rule_id, limit)}

    @router.get("/rules/{rule_id}/versions")
    async def rule_versions(rule_id: str) -> dict[str, Any]:
        rule_of(rule_id)
        return {"versions": reader.versions(rule_id)}

    @router.get("/rules/{rule_id}/diff")
    async def rule_diff(
        rule_id: str, from_: int = Query(alias="from", ge=1), to: int = Query(ge=1)
    ) -> dict[str, Any]:
        rule_of(rule_id)
        try:
            old, new = reader.version_text(rule_id, from_), reader.version_text(rule_id, to)
        except (KeyError, LookupError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="unknown version") from exc
        diff = difflib.unified_diff(
            old.splitlines(), new.splitlines(), f"v{from_}", f"v{to}", lineterm=""
        )
        return {"from": from_, "to": to, "diff": "\n".join(diff)}

    @router.get("/entities")
    async def entities() -> dict[str, Any]:
        users: dict[str, set[str]] = defaultdict(set)
        for rule in reader.rules().values():
            for eid in rule_entities(rule):
                users[eid].add(rule.id)
        out = []
        for eid in sorted(users):
            info = reader.entity(eid) or {}
            out.append(
                {
                    "entity_id": eid,
                    "name": info.get("name") or eid,
                    "area": info.get("area"),
                    "domain": eid.split(".", 1)[0],
                    "state": info.get("state"),
                    "stale": info.get("stale", False),
                    "rule_count": len(users[eid]),
                }
            )
        return {"entities": out}

    @router.get("/entities/{entity_id}/rules")
    async def entity_rules(entity_id: str) -> dict[str, Any]:
        last = _last_fired(reader)
        rows = []
        for rule in reader.rules().values():
            roles = sorted(r for r, ids in _roles(rule).items() if entity_id in ids)
            if roles:
                rows.append(_summary(reader, rule, last, _areas(reader, rule)) | {"roles": roles})
        info = reader.entity(entity_id)
        if not rows and info is None:
            raise HTTPException(status_code=404, detail=f"unknown entity {entity_id!r}")
        return {
            "entity_id": entity_id,
            "entity": info,
            "rules": sorted(rows, key=lambda r: r["id"]),
        }

    @router.get("/overview")
    async def overview(days: int = Query(default=HEATMAP_DAYS, ge=1, le=90)) -> dict[str, Any]:
        last = _last_fired(reader)
        groups: dict[str, dict[str, list[str]]] = {"area": {}, "tag": {}, "target": {}}
        summaries = []
        for rule in reader.rules().values():
            areas = _areas(reader, rule)
            summaries.append(_summary(reader, rule, last, areas))
            for key in areas or ["(ohne Area)"]:
                groups["area"].setdefault(key, []).append(rule.id)
            for tag in rule.tags or ["(ohne Tag)"]:
                groups["tag"].setdefault(tag, []).append(rule.id)
            groups["target"].setdefault(
                rule.alert.target if rule.alert else "(kein Ziel)", []
            ).append(rule.id)
        since = reader.now() - timedelta(days=days)
        heat: dict[str, dict[str, int]] = defaultdict(dict)
        for rid, day, count in reader.run_counts(since):
            owner = reader.owner(rid)
            heat[owner][day] = heat[owner].get(day, 0) + count
        return {
            "groups": {k: dict(sorted(v.items())) for k, v in groups.items()},
            "heatmap": {
                "days": days,
                "since": since.isoformat(),
                "rules": dict(sorted(heat.items())),
            },
            "highlight": {
                "shadow": sorted(s["id"] for s in summaries if s["status"] == "shadow"),
                "error": sorted(s["id"] for s in summaries if s["status"] == "error"),
            },
            "rules": sorted(summaries, key=lambda s: s["id"]),
        }

    @router.get("/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            event_stream(reader),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
