"""`state_history`: Zustandswechsel relevanter Entities (Grundlage für Backtests)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .clock import Now, utc_now
from .state import StateChange

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_history (
    entity_id TEXT NOT NULL,
    at TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS state_history_entity ON state_history (entity_id, at);
"""
_PRUNE_EVERY = timedelta(hours=1)


class StateHistory:
    def __init__(
        self,
        db_path: Path | str = ":memory:",
        relevant: Callable[[str], bool] = lambda _: True,
        retention_days: int = 14,
        now: Now = utc_now,
    ) -> None:
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._relevant, self._now = relevant, now
        self.retention = timedelta(days=retention_days)
        self._last_prune: datetime | None = None
        self.prune()

    def close(self) -> None:
        self._db.close()

    async def on_change(self, change: StateChange) -> None:
        """StateStore-Listener: nur echte Zustandswechsel relevanter Entities."""
        if change.state_changed and self._relevant(change.entity_id):
            self.record(change.entity_id, change.new.state, change.new.last_changed)

    def record(self, entity_id: str, state: str, at: datetime) -> None:
        self._db.execute(
            "INSERT INTO state_history VALUES (?, ?, ?)",
            (entity_id, at.astimezone(UTC).isoformat(), state),
        )
        self._db.commit()
        if self._last_prune is None or self._now() - self._last_prune >= _PRUNE_EVERY:
            self.prune()

    def prune(self) -> int:
        now = self._now()
        self._last_prune = now
        cur = self._db.execute(
            "DELETE FROM state_history WHERE at < ?",
            ((now - self.retention).astimezone(UTC).isoformat(),),
        )
        self._db.commit()
        return cur.rowcount

    def changes(
        self, entity_ids: list[str], start: datetime, end: datetime
    ) -> list[tuple[datetime, str, str]]:
        """`(zeit, entity_id, state)` im Intervall, chronologisch; zusätzlich der Zustand zum
        Start je Entity (zeitgestempelt auf `start`), damit Backtests mit vollem Wissen beginnen."""
        out: list[tuple[datetime, str, str]] = []
        for eid in entity_ids:
            row = self._db.execute(
                "SELECT state FROM state_history WHERE entity_id = ? AND at <= ? "
                "ORDER BY at DESC LIMIT 1",
                (eid, start.astimezone(UTC).isoformat()),
            ).fetchone()
            if row:
                out.append((start, eid, row[0]))
            rows = self._db.execute(
                "SELECT at, state FROM state_history WHERE entity_id = ? AND at > ? AND at <= ? "
                "ORDER BY at",
                (eid, start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
            ).fetchall()
            out += [(datetime.fromisoformat(at), eid, st) for at, st in rows]
        return sorted(out, key=lambda x: x[0])
