"""Laufzeit-Circuit-Breaker: zu viele Aktivierungen pro Stunde deaktivieren die Regel."""

from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from ..clock import Now

_SCHEMA = "CREATE TABLE IF NOT EXISTS rule_trips (rule_id TEXT PRIMARY KEY, at TEXT NOT NULL)"


class CircuitBreaker:
    def __init__(self, db_path: Path | str, now: Now, max_per_hour: int) -> None:
        self._now, self._max = now, max_per_hour
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)

    def close(self) -> None:
        self._db.close()

    def record(self, rule_id: str) -> bool:
        """Zählt eine Aktivierung. `True`, wenn die Regel dadurch neu ausgelöst (getrippt) hat."""
        if self.is_tripped(rule_id):
            return False
        now = self._now()
        hits = self._hits[rule_id]
        hits.append(now)
        while hits and now - hits[0] > timedelta(hours=1):
            hits.popleft()
        if len(hits) <= self._max:
            return False
        self._db.execute(
            "INSERT OR REPLACE INTO rule_trips VALUES (?, ?)", (rule_id, now.isoformat())
        )
        self._db.commit()
        hits.clear()
        return True

    def is_tripped(self, rule_id: str) -> bool:
        return rule_id in self.tripped()

    def tripped(self) -> set[str]:
        return {r for (r,) in self._db.execute("SELECT rule_id FROM rule_trips")}

    def clear(self, rule_id: str) -> None:
        self._db.execute("DELETE FROM rule_trips WHERE rule_id = ?", (rule_id,))
        self._db.commit()
        self._hits.pop(rule_id, None)
