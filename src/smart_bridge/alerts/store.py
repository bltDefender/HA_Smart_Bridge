"""AlertStore: aktive Alerts aus Engine-Ergebnissen, persistiertes `since`, Snoozes."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..clock import Now, utc_now
from ..engine import AlertActivated, AlertCleared, Outcome
from ..rules.models import Rule

MAX_ALERTS = 20
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
SYSTEM_TARGET = "building.haus"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_since (alert_id TEXT PRIMARY KEY, since TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snoozes (alert_id TEXT PRIMARY KEY, until TEXT NOT NULL);
"""

Listener = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class Alert:
    id: str
    target: str
    severity: str
    title: str
    message: str
    since: datetime
    blink: bool | None = None
    color: str | None = None
    zoom: bool | None = None

    def to_json(self, zone: Any = None) -> dict[str, Any]:
        """Schnittstelle zum 3D-Modell (fest): optionale Felder nur, wenn gesetzt."""
        since = self.since.astimezone(zone) if zone else self.since
        out: dict[str, Any] = {
            "id": self.id,
            "target": self.target,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
        }
        for key in ("blink", "color", "zoom"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        out["since"] = since.isoformat()
        return out


def _dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw).astimezone(UTC)


class AlertStore:
    def __init__(
        self,
        rules: Callable[[], dict[str, Rule]],
        db_path: Path | str = ":memory:",
        now: Now = utc_now,
    ) -> None:
        self._rules, self._now = rules, now
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._active: dict[str, Alert] = {}
        self._system: dict[str, Alert] = {}
        self._listeners: list[Listener] = []

    def close(self) -> None:
        self._db.close()

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def _changed(self) -> None:
        for listener in self._listeners:
            await listener()

    # --- Engine-Ergebnisse -----------------------------------------------

    async def handle(self, outcome: Outcome) -> None:
        if isinstance(outcome, AlertActivated):
            await self._activate(outcome)
        elif isinstance(outcome, AlertCleared):
            if self._active.pop(outcome.rule_id, None) is not None:
                self._db.execute("DELETE FROM alert_since WHERE alert_id = ?", (outcome.rule_id,))
                self._db.commit()
                await self._changed()

    async def _activate(self, outcome: AlertActivated) -> None:
        rule = self._rules().get(outcome.rule_id)
        if rule is None or rule.alert is None:
            return
        row = self._db.execute(
            "SELECT since FROM alert_since WHERE alert_id = ?", (rule.id,)
        ).fetchone()
        since = _dt(row[0]) if row else outcome.since.astimezone(UTC)
        if not row:
            self._db.execute("INSERT INTO alert_since VALUES (?, ?)", (rule.id, since.isoformat()))
            self._db.commit()
        spec = rule.alert
        self._active[rule.id] = Alert(
            id=rule.id,
            target=spec.target,
            severity=spec.severity,
            title=spec.title or rule.title or rule.id,
            message=spec.message or "",
            since=since,
            blink=spec.blink,
            color=spec.color,
            zoom=spec.zoom,
        )
        await self._changed()

    async def reconcile(self, active_ids: set[str]) -> None:
        """Nach dem Start: persistierte `since`-Werte ohne aktiven Alert verwerfen."""
        rows = self._db.execute("SELECT alert_id FROM alert_since").fetchall()
        for (alert_id,) in rows:
            if alert_id not in active_ids:
                self._db.execute("DELETE FROM alert_since WHERE alert_id = ?", (alert_id,))
        self._db.commit()

    # --- Systemalerts (z. B. abgelehnte Regeln) ----------------------------

    async def set_system(
        self, key: str, title: str, message: str, severity: str = "warning"
    ) -> None:
        alert_id = f"error:{key}"
        old = self._system.get(alert_id)
        since = old.since if old else self._now().astimezone(UTC)
        self._system[alert_id] = Alert(alert_id, SYSTEM_TARGET, severity, title, message, since)
        await self._changed()

    async def clear_system(self, key: str) -> None:
        if self._system.pop(f"error:{key}", None) is not None:
            await self._changed()

    async def replace_system(self, errors: dict[str, str]) -> None:
        """Setzt den Satz Fehler-Alerts auf genau `errors` (Regel-ID → Meldung)."""
        wanted = {f"error:{k}" for k in errors}
        for alert_id in set(self._system) - wanted:
            del self._system[alert_id]
        for key, message in errors.items():
            await self.set_system(key, f"Regel {key} abgelehnt", message)
        await self._changed()

    # --- Snooze ----------------------------------------------------------

    async def snooze(self, alert_id: str, until: datetime) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO snoozes VALUES (?, ?)",
            (alert_id, until.astimezone(UTC).isoformat()),
        )
        self._db.commit()
        await self._changed()

    def _snoozed(self, alert_id: str, now: datetime) -> bool:
        row = self._db.execute(
            "SELECT until FROM snoozes WHERE alert_id = ?", (alert_id,)
        ).fetchone()
        return bool(row) and _dt(row[0]) > now

    # --- Lesen -----------------------------------------------------------

    def alerts(self) -> list[Alert]:
        """Deterministisch nach Severity, dann `since`, dann id; höchstens 20; ohne Snoozes."""
        now = self._now().astimezone(UTC)
        pool = [*self._active.values(), *self._system.values()]
        visible = [a for a in pool if not self._snoozed(a.id, now)]
        visible.sort(key=lambda a: (_SEVERITY_ORDER[a.severity], a.since, a.id))
        return visible[:MAX_ALERTS]
