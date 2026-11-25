"""simulate_rule und backtest_rule: Wegwerf-Engine mit Snapshot-Zuständen, ohne Seiteneffekte.

Die Engine kennt kein HA; Executor, AlertStore und Netzwerk sind nicht beteiligt. Aktionen werden
nur gesammelt, nie ausgeführt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .catalog.documents import rule_entities
from .engine import Outcome, RuleEngine
from .engine.conditions import SunProvider, TraceNode
from .engine.outcomes import ActionsRequested, AlertActivated, AlertCleared
from .engine.timers import Callback
from .history import StateHistory
from .rules import parse_rules_text
from .rules.models import Action, Rule
from .state import EntityState, StateStore

STATE_TRIGGERS = frozenset({"state", "numeric_state", "event", "mqtt", "webhook", "startup"})


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ManualTimers:
    """Timer auf virtueller Zeit: `advance_to` führt fällige Callbacks in Reihenfolge aus."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._items: dict[Hashable, tuple[datetime, Callback]] = {}

    def schedule(self, key: Hashable, delay: float, callback: Callback) -> None:
        self._items[key] = (self._clock.now + timedelta(seconds=delay), callback)

    def cancel(self, key: Hashable) -> bool:
        return self._items.pop(key, None) is not None

    def cancel_all(self) -> None:
        self._items.clear()

    async def advance_to(self, target: datetime) -> None:
        while True:
            due = [(d, k) for k, (d, _) in self._items.items() if d <= target]
            if not due:
                break
            when, key = min(due, key=lambda x: x[0])
            _, callback = self._items.pop(key)
            self._clock.now = max(self._clock.now, when)
            await callback()
        self._clock.now = max(self._clock.now, target)


def trace_to_dict(node: TraceNode) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": node.kind, "result": node.result}
    if node.detail:
        out["detail"] = node.detail
    if node.children:
        out["children"] = [trace_to_dict(c) for c in node.children]
    return out


def action_summary(action: Action) -> dict[str, Any]:
    return {"type": action.type, **json.loads(json.dumps(action.model_extra or {}, default=str))}


def _unreplayable(rules: list[Rule]) -> list[str]:
    return sorted(
        {
            f"{r.id}:{t.type}"
            for r in rules
            for t in (r.triggers or [])
            if t.type not in STATE_TRIGGERS
        }
    )


@dataclass
class Env:
    zone: ZoneInfo
    sun: SunProvider | None = None
    is_workday: Callable[[date], bool] = lambda d: d.weekday() < 5


@dataclass
class _Collector:
    clock: _Clock
    items: list[tuple[datetime, Outcome]] = field(default_factory=list)

    async def __call__(self, outcome: Outcome) -> None:
        self.items.append((self.clock.now, outcome))


async def _engine(
    rule_yaml: str, env: Env, now: datetime
) -> tuple[RuleEngine, StateStore, _Clock, ManualTimers, _Collector, list[Rule], dict[str, str]]:
    rules, errors = parse_rules_text(rule_yaml)
    clock = _Clock(now)
    timers = ManualTimers(clock)
    store = StateStore(now=clock)
    sink = _Collector(clock)
    engine = RuleEngine(
        store, sink, now=clock, timers=timers, zone=env.zone, sun=env.sun, is_workday=env.is_workday
    )
    load_errors = {} if errors else engine.load(rules)
    store.subscribe(engine.on_state_change)
    return engine, store, clock, timers, sink, rules, load_errors


def _local(dt: datetime, zone: ZoneInfo) -> str:
    return dt.astimezone(zone).isoformat()


async def simulate_rule(
    rule_yaml: str,
    env: Env,
    current: Callable[[str], EntityState | None],
    history: StateHistory | None = None,
    now: datetime | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Wertet die Regel gegen den aktuellen oder den historischen Zustand zu `at` aus."""
    when = at or now
    assert when is not None
    engine, store, _, _, _, rules, load_errors = await _engine(rule_yaml, env, when)
    errors = list(parse_rules_text(rule_yaml)[1])
    if errors or not rules:
        return {"valid": False, "errors": errors or ["no rules"]}
    wanted = sorted({e for r in engine.rules.values() for e in rule_entities(r)})
    missing: list[str] = []
    if at is not None and history is not None:
        for _, eid, state in history.changes(wanted, at, at):
            await store.update(eid, state, {}, at)
    else:
        for eid in wanted:
            st = current(eid)
            if st is not None:
                await store.update(eid, st.state, dict(st.attributes), st.last_changed)
    missing = [e for e in wanted if store.get(e) is None]
    results = []
    for rid, rule in engine.rules.items():
        trace = engine.explain(rid)
        results.append(
            {
                "rule_id": rid,
                "gate": trace_to_dict(trace),
                "condition_true": trace.result,
                "alert_would_be_active": bool(rule.alert and trace.result is True),
                "for": rule.for_,
                "actions_if_triggered": [
                    action_summary(a) for a in (rule.actions or []) if trace.result is True
                ],
            }
        )
    return {
        "valid": True,
        "at": _local(when, env.zone),
        "source": "history" if at is not None else "current",
        "rules": results,
        "rejected": load_errors,
        "entities_without_state": missing,
        "side_effects": False,
    }


async def backtest_rule(
    rule_yaml: str, env: Env, history: StateHistory, now: datetime, days: int = 7
) -> dict[str, Any]:
    """Spielt `state_history` durch: Aktivierungen, Dauer und Aktionen, die gefeuert hätten."""
    start = now - timedelta(days=days)
    engine, store, clock, timers, sink, rules, load_errors = await _engine(rule_yaml, env, start)
    errors = list(parse_rules_text(rule_yaml)[1])
    if errors or not rules:
        return {"valid": False, "errors": errors or ["no rules"]}
    wanted = sorted({e for r in engine.rules.values() for e in rule_entities(r)})
    rows = history.changes(wanted, start, now)
    initial = [r for r in rows if r[0] <= start]
    later = [r for r in rows if r[0] > start]
    for _, eid, state in initial:
        await store.update(eid, state, {}, start)
    await engine.start()
    for ts, eid, state in later:
        await timers.advance_to(ts)
        await store.update(eid, state, {}, ts)
    await timers.advance_to(now)

    per_rule: dict[str, dict[str, Any]] = {}
    for rid in engine.rules:
        per_rule[rid] = {"activations": [], "fires": [], "_open": None}
    for ts, outcome in sink.items:
        entry = per_rule[outcome.rule_id]
        if isinstance(outcome, AlertActivated):
            entry["_open"] = outcome.since
        elif isinstance(outcome, AlertCleared) and entry["_open"] is not None:
            entry["activations"].append((entry["_open"], outcome.at, False))
            entry["_open"] = None
        elif isinstance(outcome, ActionsRequested) and outcome.trigger.type != "startup":
            entry["fires"].append(
                {
                    "at": _local(ts, env.zone),
                    "phase": outcome.phase,
                    "trigger": outcome.trigger.type,
                    "actions": [action_summary(a) for a in outcome.actions],
                }
            )
    out: dict[str, Any] = {}
    for rid, entry in per_rule.items():
        if entry["_open"] is not None:
            entry["activations"].append((entry["_open"], now, True))
        acts = [
            {
                "start": _local(s, env.zone),
                "end": None if ongoing else _local(e, env.zone),
                "duration_seconds": int((e - s).total_seconds()),
                "ongoing": ongoing,
            }
            for s, e, ongoing in entry["activations"]
        ]
        out[rid] = {
            "activation_count": len(acts),
            "activations": acts,
            "total_active_seconds": sum(a["duration_seconds"] for a in acts),
            "fire_count": len(entry["fires"]),
            "fires_per_day": round(len(entry["fires"]) / days, 2),
            "fires": entry["fires"],
        }
    covered = {eid for _, eid, _ in rows}
    return {
        "valid": True,
        "from": _local(start, env.zone),
        "to": _local(now, env.zone),
        "days": days,
        "rules": out,
        "rejected": load_errors,
        "entities_without_history": [e for e in wanted if e not in covered],
        "unreplayed_triggers": _unreplayable(list(engine.rules.values())),
        "notes": [
            "Nur Zustandswechsel aus state_history werden nachgespielt; Zeit-/Sonnen-Trigger und "
            "Fenstergrenzen nicht.",
        ],
    }
