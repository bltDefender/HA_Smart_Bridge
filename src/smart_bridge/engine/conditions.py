"""Condition-Auswertung mit dreiwertiger Logik (True/False/None=unbekannt) und Trace je Knoten.

`None` entsteht, wenn eine Entity nicht verfügbar ist und `on_unavailable: ignore` gilt.
Bei `deviation` zählt eine nicht verfügbare Entity als erfüllte Abweichung.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from ..rules.models import ActiveWindow, Condition, SunWindow, TimeCond
from ..state import EntityState
from ..timeutil import parse_clock, parse_duration

UNAVAILABLE = frozenset({"unavailable", "unknown"})
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class StateReader(Protocol):
    def get(self, entity_id: str) -> EntityState | None: ...


class SunProvider(Protocol):
    def event_time(self, event: str, day: date) -> datetime | None:
        """Zeitpunkt (tz-aware) von `sunrise`/`sunset` am lokalen Kalendertag."""


@dataclass(frozen=True)
class TraceNode:
    kind: str
    result: bool | None
    detail: str = ""
    children: tuple[TraceNode, ...] = ()


@dataclass(frozen=True)
class EvalContext:
    states: StateReader
    now: datetime  # tz-aware
    zone: ZoneInfo
    on_unavailable: Literal["ignore", "deviation"] = "ignore"
    sun: SunProvider | None = None
    is_workday: Callable[[date], bool] = lambda d: d.weekday() < 5

    @property
    def local_now(self) -> datetime:
        if self.now.tzinfo is None:
            raise ValueError("now must be tz-aware")
        return self.now.astimezone(self.zone)


def kleene_and(values: list[bool | None]) -> bool | None:
    if any(v is False for v in values):
        return False
    return None if any(v is None for v in values) else True


def kleene_or(values: list[bool | None]) -> bool | None:
    if any(v is True for v in values):
        return True
    return None if any(v is None for v in values) else False


def _unavailable(ctx: EvalContext) -> bool | None:
    return True if ctx.on_unavailable == "deviation" else None


def _number(st: EntityState | None) -> float | None:
    if st is None or st.state in UNAVAILABLE:
        return None
    try:
        return float(st.state)
    except ValueError:
        return None


def clock_window(
    now: datetime,
    after: str | None,
    before: str | None,
    weekdays: Sequence[str] | None,
) -> bool:
    """Uhrzeitfenster (lokale Wanduhr). Über Mitternacht zählt der Wochentag des Fensterbeginns."""
    t = now.time()
    a = parse_clock(after) if after else None
    b = parse_clock(before) if before else None
    day = now.date()
    if a and b and a > b:
        inside = t >= a or t < b
        if t < b:
            day -= timedelta(days=1)
    elif a and b:
        inside = a <= t < b
    elif a:
        inside = t >= a
    elif b:
        inside = t < b
    else:
        inside = True
    if inside and weekdays:
        inside = WEEKDAYS[day.weekday()] in weekdays
    return inside


def _sun_window(sun: SunWindow, ctx: EvalContext) -> bool | None:
    if ctx.sun is None:
        return None
    now = ctx.local_now
    lower = upper = None
    if sun.after:
        t = ctx.sun.event_time(sun.after, now.date())
        if t is None:
            return None
        lower = t + parse_duration(sun.after_offset or "00:00:00")
    if sun.before:
        t = ctx.sun.event_time(sun.before, now.date())
        if t is None:
            return None
        upper = t + parse_duration(sun.before_offset or "00:00:00")
    return (lower is None or now >= lower) and (upper is None or now < upper)


def evaluate_window(window: ActiveWindow | TimeCond, ctx: EvalContext) -> TraceNode:
    ok = clock_window(ctx.local_now, window.after, window.before, window.weekdays)
    result: bool | None = ok
    sun = getattr(window, "sun", None)
    if ok and sun is not None:
        result = _sun_window(sun, ctx)
    return TraceNode("time_window", result)


def evaluate(cond: Condition, ctx: EvalContext) -> TraceNode:
    if cond.state is not None:
        wanted = [cond.state.is_] if isinstance(cond.state.is_, str) else cond.state.is_
        st = ctx.states.get(cond.state.entity_id)
        actual = st.state if st else None
        if actual is not None and actual in wanted:
            res: bool | None = True
        elif st is None or actual in UNAVAILABLE:
            res = _unavailable(ctx)
        else:
            res = False
        return TraceNode("state", res, f"{cond.state.entity_id}={actual!r} in {wanted}")
    if cond.numeric_state is not None:
        n = cond.numeric_state
        value = _number(ctx.states.get(n.entity_id))
        if value is None:
            return TraceNode("numeric_state", _unavailable(ctx), f"{n.entity_id}=n/a")
        ok = (n.above is None or value > n.above) and (n.below is None or value < n.below)
        return TraceNode("numeric_state", ok, f"{n.entity_id}={value}")
    if cond.time is not None:
        return evaluate_window(cond.time, ctx)
    if cond.sun is not None:
        return TraceNode("sun", _sun_window(cond.sun, ctx))
    if cond.workday is not None:
        is_wd = ctx.is_workday(ctx.local_now.date())
        return TraceNode("workday", is_wd == cond.workday)
    if cond.and_ is not None:
        kids = tuple(evaluate(c, ctx) for c in cond.and_)
        return TraceNode("and", kleene_and([k.result for k in kids]), children=kids)
    if cond.or_ is not None:
        kids = tuple(evaluate(c, ctx) for c in cond.or_)
        return TraceNode("or", kleene_or([k.result for k in kids]), children=kids)
    if cond.not_ is not None:
        kid = evaluate(cond.not_, ctx)
        return TraceNode("not", None if kid.result is None else not kid.result, children=(kid,))
    raise ValueError("unsupported condition")  # template: M7


def unsupported_kinds(cond: Condition, sun_available: bool) -> set[str]:
    """Condition-Arten, die diese Engine-Konfiguration nicht auswerten kann."""
    found: set[str] = set()
    if cond.template is not None:
        found.add("template")
    if cond.sun is not None and not sun_available:
        found.add("sun")
    for child in [*(cond.and_ or []), *(cond.or_ or []), *([cond.not_] if cond.not_ else [])]:
        found |= unsupported_kinds(child, sun_available)
    return found
