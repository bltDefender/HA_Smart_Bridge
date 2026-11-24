"""Berechnung des nächsten Feuerzeitpunkts je Zeit-Trigger (rein, ohne I/O).

Alle Rückgaben sind UTC-aware. Vergleiche laufen bewusst über UTC: Python vergleicht
Datetimes mit gleicher `tzinfo` nach Wanduhr und ignoriert dabei `fold`.

DST-Regeln:
- Feste Uhrzeiten (`time`, `date`, Cron mit fester Stunde): Lücke (Vorstellen der Uhr) →
  Ausführung zur um die Lücke verschobenen Zeit; doppelte Stunde (Zurückstellen) → einmal,
  beim ersten Vorkommen.
- Muster mit Stundenjoker (`*/5 * * * *`): Lückenzeiten entfallen, in der doppelten Stunde
  feuert jedes Vorkommen. So bleibt die Frequenz in realer Zeit erhalten.
- `interval` und `sun` rechnen in absoluter Zeit.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import CroniterBadDateError, croniter

from ..rules.models import (
    CronTrigger,
    DateTrigger,
    IntervalTrigger,
    SunTrigger,
    TimePatternTrigger,
    TimeTrigger,
)
from ..timeutil import parse_clock, parse_duration
from .sun import AstralSun

TimeSpec = (
    TimeTrigger | TimePatternTrigger | CronTrigger | IntervalTrigger | DateTrigger | SunTrigger
)
NextFn = Callable[[datetime, datetime | None], datetime | None]

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MAX_CRON_STEPS = 20000


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be tz-aware")
    return dt.astimezone(UTC)


def _expand(naive: datetime, zone: ZoneInfo, wild: bool) -> list[datetime]:
    """Wanduhrzeit → UTC-Instanten unter Beachtung der DST-Regeln (siehe Moduldoku)."""
    f0 = naive.replace(tzinfo=zone, fold=0)
    f1 = naive.replace(tzinfo=zone, fold=1)
    shifted = f0.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive
    if shifted:  # Lücke
        return [] if wild else [f0.astimezone(UTC)]
    if f0.utcoffset() != f1.utcoffset():  # doppelte Stunde
        return [f0.astimezone(UTC), f1.astimezone(UTC)] if wild else [f0.astimezone(UTC)]
    return [f0.astimezone(UTC)]


def _local_instant(day: date, at: str, zone: ZoneInfo) -> datetime:
    return _expand(datetime.combine(day, parse_clock(at)), zone, wild=False)[0]


def _daily(
    at: str, weekdays: Sequence[str] | None, after: datetime, zone: ZoneInfo
) -> datetime | None:
    day = after.astimezone(zone).date() - timedelta(days=1)
    for _ in range(10):
        if weekdays is None or _WEEKDAYS[day.weekday()] in weekdays:
            cand = _local_instant(day, at, zone)
            if cand > after:
                return cand
        day += timedelta(days=1)
    return None


def _md(value: str) -> tuple[int, int]:
    text = value[-5:]
    return int(text[:2]), int(text[3:])


def _date_matches(trig: DateTrigger, day: date) -> bool:
    cur = (day.month, day.day)
    if trig.on is not None:
        if not trig.yearly:
            return day == date.fromisoformat(trig.on)
        target = _md(trig.on)
        leap = day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0)
        return cur == target or (target == (2, 29) and not leap and cur == (2, 28))
    assert trig.from_ and trig.to
    if not trig.yearly:
        return date.fromisoformat(trig.from_) <= day <= date.fromisoformat(trig.to)
    start, end = _md(trig.from_), _md(trig.to)
    return start <= cur <= end if start <= end else cur >= start or cur <= end


def _next_date(trig: DateTrigger, after: datetime, zone: ZoneInfo) -> datetime | None:
    first = after.astimezone(zone).date() - timedelta(days=1)
    if not trig.yearly:  # begrenzt: ohne Suchfenster berechnen
        lo = date.fromisoformat(trig.on or trig.from_ or "")
        hi = date.fromisoformat(trig.on or trig.to or "")
        day, last = max(first, lo), hi
    else:
        day, last = first, first + timedelta(days=800)
    while day <= last:
        if _date_matches(trig, day):
            cand = _local_instant(day, trig.at, zone)
            if cand > after:
                return cand
        day += timedelta(days=1)
    return None


def _next_sun(trig: SunTrigger, after: datetime, sun: AstralSun, zone: ZoneInfo) -> datetime | None:
    offset = parse_duration(trig.offset)
    day = after.astimezone(zone).date() - timedelta(days=2)
    for _ in range(375):  # Polartage überspringen, Ereignis im Folgemonat finden
        event = sun.event_time(trig.event, day)
        if event is not None:
            cand = _utc(event + offset)
            if cand > after:
                return cand
        day += timedelta(days=1)
    return None


def pattern_to_cron(trig: TimePatternTrigger) -> str:
    """`time_pattern` → 6-Feld-Cron (Sekunden zuletzt). Nicht genannte kleinere Felder sind 0."""

    def conv(v: str) -> str:
        return f"*{v}" if v.startswith("/") else v

    hours = conv(trig.hours) if trig.hours is not None else "*"
    if trig.minutes is not None:
        minutes = conv(trig.minutes)
    else:
        minutes = "0" if trig.hours is not None else "*"
    if trig.seconds is not None:
        seconds = conv(trig.seconds)
    else:
        seconds = "0" if (trig.hours is not None or trig.minutes is not None) else "*"
    return f"{minutes} {hours} * * * {seconds}"


def _hour_field(expression: str) -> str:
    return expression.split()[1]


def _next_cron(expression: str, after: datetime, zone: ZoneInfo) -> datetime | None:
    wild = bool(re.search(r"[*/-]", _hour_field(expression)))
    local = after.astimezone(zone)
    near_dst = (local - timedelta(hours=3)).utcoffset() != (local + timedelta(hours=3)).utcoffset()
    margin = timedelta(hours=2) if near_dst else timedelta(0)
    it = croniter(expression, local.replace(tzinfo=None) - margin)
    best: datetime | None = None
    limit: datetime | None = None
    for _ in range(_MAX_CRON_STEPS):
        try:
            naive = it.get_next(datetime)
        except CroniterBadDateError:
            break
        if limit is not None and naive > limit:
            break
        for inst in _expand(naive, zone, wild):
            if inst > after and (best is None or inst < best):
                best = inst
                limit = naive + timedelta(hours=2)
    return best


def make_next(spec: TimeSpec, zone: ZoneInfo, sun: AstralSun | None) -> NextFn:
    """Liefert `next(after, last_fire) -> UTC | None`; strikt nach `after`."""

    if isinstance(spec, SunTrigger) and sun is None:
        raise ValueError("sun trigger needs a sun provider")

    def fn(after: datetime, last: datetime | None) -> datetime | None:
        after_utc = _utc(after)
        if isinstance(spec, TimeTrigger):
            return _daily(spec.at, spec.weekdays, after_utc, zone)
        if isinstance(spec, TimePatternTrigger):
            return _next_cron(pattern_to_cron(spec), after_utc, zone)
        if isinstance(spec, CronTrigger):
            return _next_cron(spec.expression, after_utc, zone)
        if isinstance(spec, IntervalTrigger):
            every = parse_duration(spec.every)
            if last is None:
                return after_utc + every
            nxt = _utc(last) + every
            if nxt <= after_utc:  # verpasste Takte überspringen, Phase beibehalten
                nxt += every * ((after_utc - nxt) // every + 1)
            return nxt
        if isinstance(spec, DateTrigger):
            return _next_date(spec, after_utc, zone)
        assert sun is not None
        return _next_sun(spec, after_utc, sun, zone)

    return fn
