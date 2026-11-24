from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from smart_bridge.rules.models import (
    CronTrigger,
    DateTrigger,
    IntervalTrigger,
    SunTrigger,
    TimePatternTrigger,
    TimeTrigger,
)
from smart_bridge.scheduler.calendar import workday_provider
from smart_bridge.scheduler.nextfire import TimeSpec, make_next
from smart_bridge.scheduler.sun import AstralSun, sun_drift

BERLIN = ZoneInfo("Europe/Berlin")
OSLO = ZoneInfo("Europe/Oslo")


def local(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=BERLIN)


def nxt(spec: TimeSpec, after: datetime, last: datetime | None = None) -> datetime | None:
    sun = AstralSun(52.4, 13.1, BERLIN)
    res = make_next(spec, BERLIN, sun)(after, last)
    return res.astimezone(BERLIN) if res else None


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)


# --- time ----------------------------------------------------------------


def test_time_today_then_tomorrow() -> None:
    spec = TimeTrigger(type="time", at="22:30")
    assert nxt(spec, local(2026, 1, 5, 12)) == local(2026, 1, 5, 22, 30)
    assert nxt(spec, local(2026, 1, 5, 22, 30)) == local(2026, 1, 6, 22, 30)  # strikt danach


def test_time_weekdays_skip_weekend() -> None:
    spec = TimeTrigger(type="time", at="08:00", weekdays=["mon", "tue"])
    assert nxt(spec, local(2026, 1, 6, 9)) == local(2026, 1, 12, 8)  # Dienstag → Montag


def test_time_midnight_and_year_rollover() -> None:
    spec = TimeTrigger(type="time", at="00:00")
    assert nxt(spec, local(2026, 12, 31, 23, 59)) == local(2027, 1, 1, 0, 0)


def test_time_in_spring_gap_is_shifted() -> None:
    spec = TimeTrigger(type="time", at="02:30")
    res = nxt(spec, local(2026, 3, 28, 12))
    assert res is not None
    assert res.date() == date(2026, 3, 29)
    assert res.hour == 3 and res.minute == 30  # 02:30 existiert nicht → 03:30 CEST
    assert nxt(spec, res) == local(2026, 3, 30, 2, 30)


def test_time_in_fall_back_hour_fires_once() -> None:
    spec = TimeTrigger(type="time", at="02:30")
    first = nxt(spec, local(2026, 10, 24, 12))
    assert first is not None and utc(first) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    second = nxt(spec, first)
    assert second == local(2026, 10, 26, 2, 30)


def test_time_dst_day_length_is_respected() -> None:
    spec = TimeTrigger(type="time", at="12:00")
    a = nxt(spec, local(2026, 3, 28, 11))
    b = nxt(spec, a) if a else None
    assert a and b and utc(b) - utc(a) == timedelta(hours=23)  # Frühjahrstag hat 23 h


# --- cron / time_pattern -------------------------------------------------


def test_cron_daily() -> None:
    spec = CronTrigger(type="cron", expression="30 22 * * *")
    assert nxt(spec, local(2026, 1, 5, 12)) == local(2026, 1, 5, 22, 30)


def test_cron_month_rollover() -> None:
    spec = CronTrigger(type="cron", expression="0 6 1 * *")
    assert nxt(spec, local(2026, 1, 31, 12)) == local(2026, 2, 1, 6)


def test_cron_wildcard_hour_skips_gap() -> None:
    spec = CronTrigger(type="cron", expression="*/30 * * * *")
    res = nxt(spec, local(2026, 3, 29, 1, 45))
    assert res is not None and utc(res) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 03:00 CEST


def test_cron_wildcard_hour_fires_in_both_ambiguous_hours() -> None:
    spec = CronTrigger(type="cron", expression="*/30 * * * *")
    fires: list[datetime] = []
    cur = datetime(2026, 10, 25, 0, 0, tzinfo=UTC) - timedelta(minutes=1)
    for _ in range(8):
        res = make_next(spec, BERLIN, None)(cur, None)
        assert res is not None
        fires.append(res)
        cur = res
    # 02:00 CEST, 02:30 CEST, 02:00 CET, 02:30 CET: jede reale halbe Stunde genau einmal
    assert [b - a for a, b in zip(fires, fires[1:], strict=False)] == [timedelta(minutes=30)] * 7


def test_cron_fixed_hour_in_gap_is_shifted_and_once_when_ambiguous() -> None:
    spec = CronTrigger(type="cron", expression="30 2 * * *")
    gap = nxt(spec, local(2026, 3, 28, 12))
    assert gap is not None and (gap.hour, gap.minute) == (3, 30)
    amb = nxt(spec, local(2026, 10, 24, 12))
    assert amb is not None and utc(amb) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    after = nxt(spec, amb)
    assert after == local(2026, 10, 26, 2, 30)


def test_cron_impossible_date_returns_none() -> None:
    spec = CronTrigger(type="cron", expression="0 0 31 2 *")
    assert nxt(spec, local(2026, 1, 1)) is None


def test_cron_with_seconds_field() -> None:
    spec = CronTrigger(type="cron", expression="0 12 * * * 30")
    assert nxt(spec, local(2026, 1, 5, 12)) == local(2026, 1, 5, 12, 0, 30)


@pytest.mark.parametrize(
    ("kwargs", "after", "expected"),
    [
        ({"minutes": "/5"}, local(2026, 1, 5, 12, 2), local(2026, 1, 5, 12, 5)),
        ({"hours": "6"}, local(2026, 1, 5, 12), local(2026, 1, 6, 6, 0)),
        ({"hours": "6", "minutes": "15"}, local(2026, 1, 5, 12), local(2026, 1, 6, 6, 15)),
        ({"seconds": "/10"}, local(2026, 1, 5, 12, 0, 3), local(2026, 1, 5, 12, 0, 10)),
        ({"minutes": "0"}, local(2026, 1, 5, 12, 30), local(2026, 1, 5, 13, 0)),
    ],
)
def test_time_pattern(kwargs: dict[str, str], after: datetime, expected: datetime) -> None:
    assert nxt(TimePatternTrigger(type="time_pattern", **kwargs), after) == expected


# --- interval ------------------------------------------------------------


def test_interval_without_history_counts_from_now() -> None:
    spec = IntervalTrigger(type="interval", every="00:10:00")
    assert nxt(spec, local(2026, 1, 5, 12)) == local(2026, 1, 5, 12, 10)


def test_interval_keeps_phase_and_skips_missed() -> None:
    spec = IntervalTrigger(type="interval", every="00:10:00")
    last = local(2026, 1, 5, 12, 0)
    assert nxt(spec, local(2026, 1, 5, 12, 25), last) == local(2026, 1, 5, 12, 30)


def test_interval_is_absolute_across_dst() -> None:
    spec = IntervalTrigger(type="interval", every="12:00:00")
    res = nxt(spec, local(2026, 3, 28, 12), local(2026, 3, 28, 12))
    assert res is not None and utc(res) - utc(local(2026, 3, 28, 12)) == timedelta(hours=12)


# --- date ----------------------------------------------------------------


def test_date_one_off() -> None:
    spec = DateTrigger(type="date", on="2026-12-24", at="18:00")
    assert nxt(spec, local(2026, 1, 1)) == local(2026, 12, 24, 18)
    assert nxt(spec, local(2026, 12, 24, 18)) is None


def test_date_yearly_rolls_to_next_year() -> None:
    spec = DateTrigger(type="date", on="12-24", yearly=True, at="08:00")
    assert nxt(spec, local(2026, 12, 24, 9)) == local(2027, 12, 24, 8)


def test_date_yearly_feb29_falls_back_to_feb28() -> None:
    spec = DateTrigger(type="date", on="02-29", yearly=True)
    assert nxt(spec, local(2026, 1, 1)) == local(2026, 2, 28, 0)
    assert nxt(spec, local(2027, 3, 1)) == local(2028, 2, 29, 0)


def test_date_range_fires_daily_inside_only() -> None:
    spec = DateTrigger(type="date", **{"from": "2026-06-01", "to": "2026-06-03"}, at="07:00")
    assert nxt(spec, local(2026, 5, 1)) == local(2026, 6, 1, 7)
    assert nxt(spec, local(2026, 6, 1, 8)) == local(2026, 6, 2, 7)
    assert nxt(spec, local(2026, 6, 3, 8)) is None


def test_date_yearly_range_wraps_year_end() -> None:
    spec = DateTrigger(type="date", yearly=True, **{"from": "12-30", "to": "01-02"}, at="07:00")
    assert nxt(spec, local(2026, 12, 31, 8)) == local(2027, 1, 1, 7)
    assert nxt(spec, local(2027, 1, 2, 8)) == local(2027, 12, 30, 7)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"on": "2026-13-01"},
        {"on": "12-24"},
        {"on": "2026-01-01", "yearly": True, "from": "01-01", "to": "01-02"},
        {"from": "2026-06-03", "to": "2026-06-01"},
        {"from": "2026-06-01"},
    ],
)
def test_date_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        DateTrigger(type="date", **kwargs)


# --- sun -----------------------------------------------------------------


def test_sun_sunset_with_negative_offset() -> None:
    sun = AstralSun(52.4, 13.1, BERLIN)
    day = date(2026, 6, 21)
    sunset = sun.event_time("sunset", day)
    assert sunset is not None
    spec = SunTrigger(type="sun", event="sunset", offset="-00:15:00")
    res = nxt(spec, local(2026, 6, 21, 12))
    assert res == (sunset - timedelta(minutes=15)).astimezone(BERLIN)
    assert (sunset.hour, sunset.minute) >= (21, 0)  # Berlin Sommer: spät


def test_sun_event_crossing_midnight_offset_uses_next_day() -> None:
    spec = SunTrigger(type="sun", event="sunset", offset="05:00:00")
    res = nxt(spec, local(2026, 6, 21, 12))
    assert res is not None and res.date() == date(2026, 6, 22)


def test_sun_dawn_before_sunrise() -> None:
    dawn = nxt(SunTrigger(type="sun", event="civil_dawn"), local(2026, 1, 5, 0, 5))
    rise = nxt(SunTrigger(type="sun", event="sunrise"), local(2026, 1, 5, 0, 5))
    assert dawn and rise and dawn < rise


def test_sun_polar_day_finds_next_valid_event() -> None:
    zone = ZoneInfo("Europe/Oslo")
    sun = AstralSun(69.65, 18.96, zone)  # Tromsø: Mitternachtssonne im Juni
    assert sun.event_time("sunset", date(2026, 6, 1)) is None
    spec = SunTrigger(type="sun", event="sunset")
    res = make_next(spec, zone, sun)(datetime(2026, 6, 1, 12, tzinfo=zone), None)
    assert res is not None and res.astimezone(zone).date() > date(2026, 7, 1)


def test_sun_polar_night_has_no_sunrise() -> None:
    sun = AstralSun(69.65, 18.96, ZoneInfo("Europe/Oslo"))
    assert sun.event_time("sunrise", date(2026, 12, 20)) is None


def test_sun_trigger_without_provider_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_next(SunTrigger(type="sun", event="sunset"), BERLIN, None)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_next(TimeTrigger(type="time", at="10:00"), BERLIN, None)(datetime(2026, 1, 1), None)


# --- Kalender / sun.sun ------------------------------------------------


def test_workday_brandenburg_vs_berlin() -> None:
    reformation = date(2025, 10, 31)  # Freitag
    assert workday_provider("DE", "BB")(reformation) is False
    assert workday_provider("DE", "BE")(reformation) is True
    assert workday_provider("DE", "BB")(date(2025, 11, 1)) is False  # Samstag
    assert workday_provider("DE", "BB")(date(2025, 10, 30)) is True


def test_sun_drift_detects_deviation() -> None:
    sun = AstralSun(52.4, 13.1, BERLIN)
    real = sun.event_time("sunset", date(2026, 6, 21))
    assert real is not None
    tol = timedelta(minutes=5)
    assert sun_drift(real.isoformat(), sun, BERLIN, tol) is None
    shifted = (real + timedelta(minutes=30)).isoformat()
    drift = sun_drift(shifted, sun, BERLIN, tol)
    assert drift is not None and abs(drift + timedelta(minutes=30)) < timedelta(seconds=1)
