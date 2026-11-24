from __future__ import annotations

from collections.abc import Callable
from datetime import date

import holidays


def workday_provider(country: str = "DE", subdiv: str | None = "BB") -> Callable[[date], bool]:
    """Werktag = Montag bis Freitag und kein gesetzlicher Feiertag (Standard Brandenburg)."""
    cal = holidays.country_holidays(country, subdiv=subdiv or None)

    def is_workday(day: date) -> bool:
        return day.weekday() < 5 and day not in cal

    return is_workday
