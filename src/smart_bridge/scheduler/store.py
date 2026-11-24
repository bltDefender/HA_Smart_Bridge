"""Persistierte Feuerzeiten (SQLite-Tabelle `fire_times`)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fire_times (
    rule_id TEXT NOT NULL,
    trigger_idx INTEGER NOT NULL,
    next_fire TEXT,
    last_fire TEXT,
    spec_hash TEXT NOT NULL,
    PRIMARY KEY (rule_id, trigger_idx)
)
"""


@dataclass(frozen=True)
class FireRecord:
    next_fire: datetime | None
    last_fire: datetime | None
    spec_hash: str


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw).astimezone(UTC) if raw else None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


class FireTimeStore:
    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def get(self, rule_id: str, idx: int) -> FireRecord | None:
        row = self._db.execute(
            "SELECT next_fire, last_fire, spec_hash FROM fire_times"
            " WHERE rule_id = ? AND trigger_idx = ?",
            (rule_id, idx),
        ).fetchone()
        return FireRecord(_dt(row[0]), _dt(row[1]), row[2]) if row else None

    def put(self, rule_id: str, idx: int, rec: FireRecord) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO fire_times VALUES (?, ?, ?, ?, ?)",
            (rule_id, idx, _iso(rec.next_fire), _iso(rec.last_fire), rec.spec_hash),
        )
        self._db.commit()

    def prune(self, keep: set[tuple[str, int]]) -> None:
        """Entfernt Einträge gelöschter Regeln/Trigger."""
        rows = self._db.execute("SELECT rule_id, trigger_idx FROM fire_times").fetchall()
        for rule_id, idx in rows:
            if (rule_id, idx) not in keep:
                self._db.execute(
                    "DELETE FROM fire_times WHERE rule_id = ? AND trigger_idx = ?",
                    (rule_id, idx),
                )
        self._db.commit()
