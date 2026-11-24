"""StateStore: In-Memory-Zustand mit SQLite-Snapshot und Stale-Handling.

Stale-Wechsel sind keine Zustandsänderung und erzeugen kein `StateChange`.
SQLite-Zugriffe sind kurze, synchrone Write-Through-Operationen.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..clock import Now, utc_now


@dataclass(frozen=True)
class EntityState:
    entity_id: str
    state: str
    attributes: dict[str, Any]
    last_changed: datetime
    stale: bool = False


@dataclass(frozen=True)
class StateChange:
    entity_id: str
    old: EntityState | None
    new: EntityState

    @property
    def state_changed(self) -> bool:
        return self.old is None or self.old.state != self.new.state


Listener = Callable[[StateChange], Awaitable[None]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_snapshot (
    entity_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    attributes TEXT NOT NULL,
    last_changed TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0
)
"""


class StateStore:
    def __init__(self, db_path: Path | str = ":memory:", now: Now = utc_now) -> None:
        self._now = now
        self._states: dict[str, EntityState] = {}
        self._listeners: list[Listener] = []
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()
        self._load()

    def _load(self) -> None:
        # Nach Neustart ist alles stale, bis die Vollsynchronisation lief.
        for eid, state, attrs, changed, _ in self._db.execute("SELECT * FROM state_snapshot"):
            self._states[eid] = EntityState(
                eid, state, json.loads(attrs), datetime.fromisoformat(changed), stale=True
            )

    def close(self) -> None:
        self._db.close()

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def get(self, entity_id: str) -> EntityState | None:
        return self._states.get(entity_id)

    def all(self) -> dict[str, EntityState]:
        return dict(self._states)

    def _persist(self, st: EntityState) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO state_snapshot VALUES (?, ?, ?, ?, ?)",
            (
                st.entity_id,
                st.state,
                json.dumps(st.attributes, sort_keys=True),
                st.last_changed.isoformat(),
                int(st.stale),
            ),
        )

    def _apply(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, Any],
        last_changed: datetime | None,
    ) -> StateChange | None:
        old = self._states.get(entity_id)
        if last_changed is not None and last_changed.tzinfo is None:
            raise ValueError("last_changed must be tz-aware")
        if old is not None and old.state == state:
            changed_at = old.last_changed
        else:
            changed_at = last_changed or self._now()
        new = EntityState(entity_id, state, attributes, changed_at, stale=False)
        self._states[entity_id] = new
        self._persist(new)
        if old is not None and old.state == state and old.attributes == attributes:
            return None  # nur Stale-Flag oder identisches Update
        return StateChange(entity_id, old, new)

    async def _emit(self, changes: Iterable[StateChange]) -> None:
        for change in changes:
            for listener in self._listeners:
                await listener(change)

    async def update(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, Any] | None = None,
        last_changed: datetime | None = None,
    ) -> StateChange | None:
        change = self._apply(entity_id, state, attributes or {}, last_changed)
        self._db.commit()
        if change:
            await self._emit([change])
        return change

    async def replace_all(self, states: Iterable[EntityState]) -> list[StateChange]:
        """Vollsync: übernimmt alle Zustände, hebt stale auf, entfernt verschwundene Entities."""
        incoming = list(states)
        changes = [
            c
            for s in incoming
            if (c := self._apply(s.entity_id, s.state, s.attributes, s.last_changed)) is not None
        ]
        keep = {s.entity_id for s in incoming}
        for gone in [e for e in self._states if e not in keep]:
            del self._states[gone]
            self._db.execute("DELETE FROM state_snapshot WHERE entity_id = ?", (gone,))
        self._db.commit()
        await self._emit(changes)
        return changes

    def remove(self, entity_id: str) -> None:
        self._states.pop(entity_id, None)
        self._db.execute("DELETE FROM state_snapshot WHERE entity_id = ?", (entity_id,))
        self._db.commit()

    def mark_all_stale(self) -> None:
        for eid, st in list(self._states.items()):
            if not st.stale:
                self._states[eid] = replace(st, stale=True)
                self._persist(self._states[eid])
        self._db.commit()
