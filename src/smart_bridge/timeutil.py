"""Parser für Uhrzeiten und Dauern aus dem Regel-YAML."""

from __future__ import annotations

import re
from datetime import time, timedelta

_DUR = re.compile(r"^(-?)(\d{1,2}):(\d{2}):(\d{2})$")
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_duration(value: str) -> timedelta:
    m = _DUR.match(value)
    if not m:
        raise ValueError(f"invalid duration: {value!r}")
    delta = timedelta(hours=int(m[2]), minutes=int(m[3]), seconds=int(m[4]))
    return -delta if m[1] else delta


def parse_clock(value: str) -> time:
    m = _CLOCK.match(value)
    if not m:
        raise ValueError(f"invalid time of day: {value!r}")
    return time(int(m[1]), int(m[2]), int(m[3] or 0))
