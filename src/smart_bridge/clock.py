"""Injizierbare Zeit; immer tz-aware."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Now = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)
