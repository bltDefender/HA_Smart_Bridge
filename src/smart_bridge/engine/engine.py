"""RuleEngine: wertet nur betroffene Regeln aus (Trigger-Index), kennt weder HA noch Netzwerk.

Semantik:
- `actions` laufen, wenn ein Trigger feuert und die Condition (inkl. `active`) wahr ist.
  Ohne `triggers` feuert jede Zustandsänderung einer Entity der Condition (abgeleitet).
- Alert, `on_activate`, `on_clear` sind level-basiert: Übergänge des Zustands "Condition wahr
  (für `for`)". Dafür werden die Entities der Condition auch bei expliziten Triggern beobachtet.
- Zeit-Trigger, `mode`, `debounce`, Rate-Limits und `repeat_after` gehören zu M4/M7.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..clock import Now
from ..events import BridgeEvent
from ..rules.models import (
    Action,
    EventTrigger,
    MqttTrigger,
    NumericStateTrigger,
    Rule,
    StateTrigger,
)
from ..state import StateChange
from ..timeutil import parse_duration
from .conditions import (
    UNAVAILABLE,
    EvalContext,
    StateReader,
    SunProvider,
    TraceNode,
    evaluate,
    evaluate_window,
    kleene_and,
    unsupported_kinds,
)
from .index import TriggerIndex, build_index, has_level
from .outcomes import (
    ActionsRequested,
    AlertActivated,
    AlertCleared,
    OutcomeSink,
    Phase,
    TriggerInfo,
)
from .timers import Timers
from .triggers import data_matches, numeric_rearmed, numeric_satisfied

log = logging.getLogger(__name__)


@dataclass
class RuleState:
    active: bool = False
    since: datetime | None = None
    pending: bool = False  # `for`-Timer läuft


def _default_workday(day: date) -> bool:
    return day.weekday() < 5


class RuleEngine:
    def __init__(
        self,
        states: StateReader,
        sink: OutcomeSink,
        *,
        now: Now,
        timers: Timers,
        zone: ZoneInfo,
        sun: SunProvider | None = None,
        is_workday: Callable[[date], bool] = _default_workday,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._states, self._sink = states, sink
        self._now, self._timers, self._zone = now, timers, zone
        self._sun, self._is_workday, self._new_id = sun, is_workday, new_id
        self._rules: dict[str, Rule] = {}
        self._state: dict[str, RuleState] = {}
        self._armed: dict[tuple[str, int, str], bool] = {}
        self.index = TriggerIndex()
        self._started = False

    # --- Laden ----------------------------------------------------------

    def load(self, rules: list[Rule]) -> dict[str, str]:
        """Ersetzt den Regelsatz. Gibt `rule_id -> Fehler` für nicht ladbare Regeln zurück.

        Laufzeitzustand bleibt für unveränderte IDs erhalten; Timer werden verworfen.
        Danach `evaluate_all()` aufrufen.
        """
        self._timers.cancel_all()
        errors: dict[str, str] = {}
        loaded: list[Rule] = []
        for rule in rules:
            if not rule.enabled or rule.status in ("disabled", "error"):
                continue
            problem = self._unsupported(rule)
            if problem:
                errors[rule.id] = problem
            else:
                loaded.append(rule)
        self._rules = {r.id: r for r in loaded}
        self._state = {rid: self._state.get(rid, RuleState()) for rid in self._rules}
        for st in self._state.values():
            st.pending = False
        self._armed.clear()
        self.index = build_index(loaded)
        return errors

    def _unsupported(self, rule: Rule) -> str | None:
        if rule.per_entity:
            return "per_entity is not supported yet (needs templates, M7)"
        if rule.condition:
            kinds = unsupported_kinds(rule.condition, self._sun is not None)
            if kinds:
                return f"unsupported condition kinds: {sorted(kinds)}"
        if rule.active and rule.active.sun and self._sun is None:
            return "active.sun needs a sun provider"
        return None

    def alert_state(self, rule_id: str) -> RuleState:
        return self._state[rule_id]

    # --- Eingänge (Event-Listener) -------------------------------------

    async def start(self) -> None:
        """Nach der Vollsynchronisation: Level-Zustände herstellen, `startup`-Trigger feuern."""
        self._started = True
        await self.evaluate_all()
        for entry in self.index.startup:
            await self._fire(self._rules[entry.rule_id], TriggerInfo("startup", self._now()))

    async def evaluate_all(self) -> None:
        for rule in self._rules.values():
            if has_level(rule):
                await self._level(rule)

    async def reevaluate(self, rule_id: str) -> None:
        """Für den Scheduler (M4): z. B. wenn ein `active`-Fenster beginnt oder endet."""
        rule = self._rules.get(rule_id)
        if rule and has_level(rule):
            await self._level(rule)

    async def on_state_change(self, change: StateChange) -> None:
        if not self._started:
            return  # Vollsync-Differenzen beim Start sind keine Auslöser
        for entry in list(self.index.entity.get(change.entity_id, [])):
            rule = self._rules[entry.rule_id]
            if entry.role == "level":
                if change.state_changed:
                    await self._level(rule)
            elif entry.role == "derived":
                if change.state_changed and change.old is not None:
                    info = TriggerInfo("level", self._now(), None, change.entity_id)
                    await self._fire(rule, info)
            else:
                assert entry.index is not None and rule.triggers
                await self._explicit_state_trigger(rule, entry.index, change)

    async def on_event(self, event: BridgeEvent) -> None:
        if not self._started:
            return
        if event.source == "webhook":
            entries = [("webhook", e) for e in self.index.webhook.get(event.data["webhook_id"], [])]
        elif event.source == "mqtt":
            entries = [("mqtt", e) for _, e in self.index.mqtt(event.data["topic"])]
        else:
            entries = [("event", e) for e in self.index.event.get(event.type, [])]
        for kind, entry in entries:
            rule = self._rules[entry.rule_id]
            assert entry.index is not None and rule.triggers
            trig = rule.triggers[entry.index]
            if isinstance(trig, EventTrigger) and not data_matches(trig.event_data, event.data):
                continue
            if isinstance(trig, MqttTrigger) and trig.payload not in (None, event.data["payload"]):
                continue
            info = TriggerInfo(kind, self._now(), entry.index, None, dict(event.data))
            await self._fire(rule, info)

    # --- State-/numeric_state-Trigger ----------------------------------

    async def _explicit_state_trigger(self, rule: Rule, idx: int, change: StateChange) -> None:
        assert rule.triggers
        trig = rule.triggers[idx]
        timer_key = ("trigger-for", rule.id, idx, change.entity_id)
        if isinstance(trig, StateTrigger):
            if not change.state_changed or change.old is None:
                return  # erstes Auftauchen ist kein Übergang
            self._timers.cancel(timer_key)
            if trig.from_ is not None and change.old.state != trig.from_:
                return
            if trig.to is not None and change.new.state != trig.to:
                return
            await self._fire_or_wait(rule, idx, trig.for_, timer_key, change)
        elif isinstance(trig, NumericStateTrigger):
            await self._numeric_trigger(rule, idx, trig, timer_key, change)

    async def _numeric_trigger(
        self,
        rule: Rule,
        idx: int,
        trig: NumericStateTrigger,
        timer_key: tuple[Any, ...],
        change: StateChange,
    ) -> None:
        if change.new.state in UNAVAILABLE:
            return  # Ausfälle weder auslösen noch neu scharf schalten
        try:
            value = float(change.new.state)
        except ValueError:
            return
        key = (rule.id, idx, change.entity_id)
        if key not in self._armed:
            ref = self._as_float(change.old.state) if change.old else None
            self._armed[key] = True if ref is None else not numeric_satisfied(trig, ref)
        if not numeric_satisfied(trig, value):
            self._timers.cancel(timer_key)
            if numeric_rearmed(trig, value):
                self._armed[key] = True
            return
        if change.old is None:
            self._armed[key] = False  # erster Wert ist kein Übergang
            return
        if self._armed[key]:
            self._armed[key] = False
            await self._fire_or_wait(rule, idx, trig.for_, timer_key, change)

    @staticmethod
    def _as_float(raw: str) -> float | None:
        try:
            return float(raw)
        except ValueError:
            return None

    async def _fire_or_wait(
        self,
        rule: Rule,
        idx: int,
        hold: str | None,
        timer_key: tuple[Any, ...],
        change: StateChange,
    ) -> None:
        info = TriggerInfo(
            rule.triggers[idx].type if rule.triggers else "state",
            self._now(),
            idx,
            change.entity_id,
            {"from": change.old.state if change.old else None, "to": change.new.state},
        )
        if not hold:
            await self._fire(rule, info)
            return

        async def elapsed() -> None:
            await self._fire(
                rule, TriggerInfo(info.type, self._now(), idx, info.entity_id, info.data)
            )

        self._timers.schedule(timer_key, parse_duration(hold).total_seconds(), elapsed)

    # --- Auswertung ----------------------------------------------------

    def _ctx(self, rule: Rule) -> EvalContext:
        return EvalContext(
            self._states,
            self._now(),
            self._zone,
            rule.on_unavailable,
            self._sun,
            self._is_workday,
        )

    def _gate(self, rule: Rule) -> TraceNode:
        """`active`-Fenster UND Condition."""
        ctx = self._ctx(rule)
        parts: list[TraceNode] = []
        if rule.active is not None:
            parts.append(evaluate_window(rule.active, ctx))
        if rule.condition is not None:
            parts.append(evaluate(rule.condition, ctx))
        result = kleene_and([p.result for p in parts])
        return TraceNode("rule", result, children=tuple(parts))

    async def _fire(self, rule: Rule, info: TriggerInfo) -> None:
        trace = self._gate(rule)
        if has_level(rule):
            await self._level(rule, trace)
        if rule.actions and trace.result is True:
            await self._request(rule, "actions", rule.actions, info, trace)

    async def _request(
        self,
        rule: Rule,
        phase: Phase,
        actions: list[Action],
        info: TriggerInfo,
        trace: TraceNode,
    ) -> None:
        await self._sink(
            ActionsRequested(
                rule.id,
                self._new_id(),
                phase,
                tuple(actions),
                info,
                trace,
                shadow=rule.status == "shadow",
            )
        )

    # --- Level-Zustand (Alert, on_activate, on_clear) --------------------

    async def _level(self, rule: Rule, trace: TraceNode | None = None) -> None:
        trace = trace or self._gate(rule)
        st = self._state[rule.id]
        timer_key = ("for", rule.id)
        if trace.result is None:
            return  # unbekannt: Zustand halten
        if trace.result:
            if st.active or st.pending:
                return
            if rule.for_:
                st.pending = True

                async def elapsed() -> None:
                    st.pending = False
                    gate = self._gate(rule)
                    if gate.result is True and not st.active:
                        await self._activate(rule, gate)

                self._timers.schedule(timer_key, parse_duration(rule.for_).total_seconds(), elapsed)
                return
            await self._activate(rule, trace)
            return
        self._timers.cancel(timer_key)
        st.pending = False
        if st.active:
            await self._clear(rule, trace)

    async def _activate(self, rule: Rule, trace: TraceNode) -> None:
        st = self._state[rule.id]
        st.active, st.since = True, self._now()
        if rule.alert:
            await self._sink(AlertActivated(rule.id, st.since, trace))
        if rule.on_activate:
            info = TriggerInfo("level", st.since)
            await self._request(rule, "on_activate", rule.on_activate, info, trace)

    async def _clear(self, rule: Rule, trace: TraceNode) -> None:
        st = self._state[rule.id]
        st.active, st.since = False, None
        now = self._now()
        if rule.alert:
            await self._sink(AlertCleared(rule.id, now, trace))
        if rule.on_clear:
            await self._request(rule, "on_clear", rule.on_clear, TriggerInfo("level", now), trace)


__all__ = ["RuleEngine", "RuleState"]
