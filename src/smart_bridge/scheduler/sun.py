"""Sonnenstand lokal via astral (läuft ohne HA), optionaler Abgleich mit `sun.sun`."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import dawn, dusk, sunrise, sunset

log = logging.getLogger(__name__)

_DEPRESSION = {"civil": 6.0, "nautical": 12.0, "astronomical": 18.0}


class AstralSun:
    def __init__(self, latitude: float, longitude: float, zone: ZoneInfo) -> None:
        self._observer = Observer(latitude, longitude)
        self._zone = zone

    def event_time(self, event: str, day: date) -> datetime | None:
        """Ereigniszeit am lokalen Kalendertag; `None` bei Polartag/-nacht."""
        try:
            if event == "sunrise":
                return sunrise(self._observer, day, tzinfo=self._zone)
            if event == "sunset":
                return sunset(self._observer, day, tzinfo=self._zone)
            kind, _, which = event.partition("_")
            depression = _DEPRESSION[kind]
            fn = dawn if which == "dawn" else dusk
            return fn(self._observer, day, depression=depression, tzinfo=self._zone)
        except ValueError:
            return None


def sun_drift(
    next_setting_iso: str, sun: AstralSun, zone: ZoneInfo, tolerance: timedelta
) -> timedelta | None:
    """Abweichung zwischen HA (`sun.sun` → `next_setting`) und der lokalen Berechnung.

    Gibt `None` zurück, wenn sie innerhalb der Toleranz liegt oder nicht berechenbar ist.
    """
    ha = datetime.fromisoformat(next_setting_iso).astimezone(UTC)
    local = sun.event_time("sunset", ha.astimezone(zone).date())
    if local is None:
        return None
    diff = local.astimezone(UTC) - ha
    return diff if abs(diff) > tolerance else None
