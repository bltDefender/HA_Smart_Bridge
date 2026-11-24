"""Zeit-Scheduler: berechnet je Trigger den nächsten Feuerzeitpunkt und schläft bis dahin.

Kein Sekunden-Polling. Lange Wartezeiten werden in Schritten von höchstens einer Stunde
geschlafen und beim Aufwachen gegen die Wanduhr geprüft; Uhrsprünge und Suspend bleiben
dadurch wirkungslos bzw. werden nachgeholt.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..clock import Now
from ..engine.index import has_level
from ..engine.timers import Timers
from ..rules.models import (
    Condition,
    Rule,
    SunTrigger,
    TimeTrigger,
)
from ..timeutil import parse_duration
from .nextfire import NextFn, TimeSpec, make_next
from .store import FireRecord, FireTimeStore
from .sun import AstralSun

log = logging.getLogger(__name__)

TriggerHandler = Callable[[str, int, datetime, dict[str, Any]], Awaitable[None]]
WindowHandler = Callable[[str], Awaitable[None]]

_TIME_TYPES = ("time", "time_pattern", "cron", "interval", "date", "sun")
_MAX_SLEEP = 3600.0


@dataclass
class _Job:
    key: tuple[str, str, int]
    rule_id: str
    next_fn: NextFn
    idx: int = 0
    spec: TimeSpec | None = None  # nur bei Trigger-Jobs; Fenstergrenzen haben keine Policy
    spec_hash: str = ""
    next_fire: datetime | None = None
    last_fire: datetime | None = None


@dataclass
class LoadReport:
    errors: dict[str, str] = field(default_factory=dict)


def _hash(spec: TimeSpec) -> str:
    return hashlib.sha256(spec.model_dump_json(by_alias=True).encode()).hexdigest()[:16]


def _conditions(cond: Condition | None) -> list[Condition]:
    if cond is None:
        return []
    out = [cond]
    for child in [*(cond.and_ or []), *(cond.or_ or []), *([cond.not_] if cond.not_ else [])]:
        out += _conditions(child)
    return out


def window_boundaries(rule: Rule) -> list[TimeSpec]:
    """Zeitpunkte, an denen sich eine rein zeitabhängige Bedingung ändern kann."""
    specs: list[TimeSpec] = []

    def clock(at: str | None) -> None:
        if at:
            specs.append(TimeTrigger(type="time", at=at))

    def sun(window: Any) -> None:
        for event, offset in (
            (window.after, window.after_offset),
            (window.before, window.before_offset),
        ):
            if event:
                specs.append(SunTrigger(type="sun", event=event, offset=offset or "00:00:00"))

    midnight = False
    if rule.active:
        clock(rule.active.after)
        clock(rule.active.before)
        midnight = midnight or rule.active.weekdays is not None
        if rule.active.sun:
            sun(rule.active.sun)
    for cond in _conditions(rule.condition):
        if cond.time:
            clock(cond.time.after)
            clock(cond.time.before)
            midnight = midnight or cond.time.weekdays is not None
        if cond.sun:
            sun(cond.sun)
        midnight = midnight or cond.workday is not None
    if midnight:
        clock("00:00")
    return specs


class Scheduler:
    def __init__(
        self,
        *,
        now: Now,
        timers: Timers,
        zone: ZoneInfo,
        store: FireTimeStore,
        on_trigger: TriggerHandler,
        on_window: WindowHandler,
        sun: AstralSun | None = None,
    ) -> None:
        self._clock, self._timers, self._zone = now, timers, zone
        self._store, self._sun = store, sun
        self._on_trigger, self._on_window = on_trigger, on_window
        self._jobs: dict[tuple[str, str, int], _Job] = {}
        self._running = False

    def _utcnow(self) -> datetime:
        return self._clock().astimezone(UTC)  # Vergleiche immer in UTC (siehe nextfire)

    # --- Laden ------------------------------------------------------------

    def load(self, rules: list[Rule]) -> LoadReport:
        """Ersetzt alle Jobs. Regeln mit nicht berechenbaren Triggern landen im Report."""
        self._timers.cancel_all()
        self._jobs.clear()
        report = LoadReport()
        for rule in rules:
            try:
                self._add_rule(rule)
            except ValueError as exc:
                report.errors[rule.id] = str(exc)
                for key in [k for k in self._jobs if k[1] == rule.id]:
                    del self._jobs[key]
        keep = {(j.rule_id, j.idx) for j in self._jobs.values() if j.spec is not None}
        self._store.prune(keep)
        if self._running:
            self._arm_all()
        return report

    def _add_rule(self, rule: Rule) -> None:
        for idx, trig in enumerate(rule.triggers or []):
            if trig.type in _TIME_TYPES:
                spec: TimeSpec = trig  # type: ignore[assignment]
                job = _Job(
                    ("trigger", rule.id, idx),
                    rule.id,
                    make_next(spec, self._zone, self._sun),
                    idx,
                    spec,
                    _hash(spec),
                )
                self._jobs[job.key] = job
        if has_level(rule):
            for n, spec in enumerate(window_boundaries(rule)):
                key = ("window", rule.id, n)
                self._jobs[key] = _Job(key, rule.id, make_next(spec, self._zone, self._sun), n)

    @property
    def next_fires(self) -> dict[tuple[str, int], datetime | None]:
        return {(j.rule_id, j.idx): j.next_fire for j in self._jobs.values() if j.spec}

    # --- Start / Missed-Fire ------------------------------------------------

    async def start(self) -> None:
        """Holt verpasste Feuerzeiten gemäß `catch_up` nach und plant alle Jobs."""
        self._running = True
        now = self._utcnow()
        catch_ups: list[tuple[_Job, datetime]] = []
        for job in self._jobs.values():
            if job.spec is None:
                job.next_fire = job.next_fn(now, None)
                continue
            rec = self._store.get(job.rule_id, job.idx)
            match = rec is not None and rec.spec_hash == job.spec_hash
            job.last_fire = rec.last_fire if rec and match else None
            if rec and match and rec.next_fire is not None:
                if rec.next_fire > now:
                    job.next_fire = rec.next_fire
                else:
                    if self._should_catch_up(job.spec, rec.next_fire, now):
                        catch_ups.append((job, rec.next_fire))
                    job.next_fire = job.next_fn(now, rec.next_fire)
            else:
                job.next_fire = job.next_fn(now, job.last_fire)
            self._persist(job)
        self._arm_all()
        for job, scheduled in catch_ups:
            job.last_fire = now
            self._persist(job)
            await self._safe_trigger(job, scheduled, {"catch_up": True, "scheduled_at": scheduled})

    @staticmethod
    def _should_catch_up(spec: TimeSpec, missed: datetime, now: datetime) -> bool:
        if spec.catch_up == "once":
            return True
        if spec.catch_up == "within" and spec.catch_up_within:
            return now - missed <= parse_duration(spec.catch_up_within)
        return False

    def stop(self) -> None:
        self._running = False
        self._timers.cancel_all()

    # --- Armieren / Feuern --------------------------------------------------

    def _persist(self, job: _Job) -> None:
        if job.spec is not None:
            self._store.put(
                job.rule_id, job.idx, FireRecord(job.next_fire, job.last_fire, job.spec_hash)
            )

    def _arm_all(self) -> None:
        for job in self._jobs.values():
            if job.next_fire is None and job.spec is None:
                job.next_fire = job.next_fn(self._utcnow(), None)
            self._arm(job)

    def _arm(self, job: _Job) -> None:
        if job.next_fire is None:
            self._timers.cancel(job.key)
            return
        delay = max(0.0, (job.next_fire - self._utcnow()).total_seconds())

        async def wake() -> None:
            await self._wake(job)

        self._timers.schedule(job.key, min(delay, _MAX_SLEEP), wake)

    async def _wake(self, job: _Job) -> None:
        if self._jobs.get(job.key) is not job:
            return
        now = self._utcnow()
        if job.next_fire is None or now < job.next_fire:
            self._arm(job)
            return
        scheduled = job.next_fire
        job.last_fire = now
        job.next_fire = job.next_fn(max(scheduled, now), scheduled)
        self._persist(job)
        self._arm(job)
        if job.spec is None:
            await self._safe_window(job)
        else:
            await self._safe_trigger(job, scheduled, {})

    async def _safe_trigger(self, job: _Job, at: datetime, data: dict[str, Any]) -> None:
        try:
            await self._on_trigger(job.rule_id, job.idx, at, data)
        except Exception:
            log.exception("time trigger failed: %s[%d]", job.rule_id, job.idx)

    async def _safe_window(self, job: _Job) -> None:
        try:
            await self._on_window(job.rule_id)
        except Exception:
            log.exception("window reevaluation failed: %s", job.rule_id)
