"""`rule_runs`: Persistenz der Läufe, plus kleine Flag-Tabelle (Kill-Switch)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_runs (
    run_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    trigger TEXT NOT NULL,
    condition_trace TEXT NOT NULL,
    actions TEXT NOT NULL,
    result TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    executor TEXT NOT NULL,
    started_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rule_runs_rule ON rule_runs (rule_id, started_at);
CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass
class StepResult:
    path: str
    type: str
    status: str  # ok | error | skipped | dry_run | shadow
    attempts: int = 0
    duration_ms: int = 0
    executor: str | None = None
    error: str | None = None
    detail: str | None = None
    idempotency_key: str | None = None


@dataclass
class RunRecord:
    run_id: str
    rule_id: str
    phase: str
    trigger: dict[str, Any]
    condition_trace: dict[str, Any]
    result: str  # ok | error | shadow | dry_run | paused
    started_at: datetime
    duration_ms: int = 0
    executor: str = "-"
    steps: list[StepResult] = field(default_factory=list)


class RunStore:
    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def save(self, run: RunRecord) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO rule_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.rule_id,
                run.phase,
                json.dumps(run.trigger, default=str),
                json.dumps(run.condition_trace),
                json.dumps([asdict(s) for s in run.steps]),
                run.result,
                run.duration_ms,
                run.executor,
                run.started_at.astimezone(UTC).isoformat(),
            ),
        )
        self._db.commit()

    def recent(self, rule_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM rule_runs"
        args: tuple[Any, ...] = ()
        if rule_id:
            query += " WHERE rule_id = ?"
            args = (rule_id,)
        cur = self._db.execute(
            query + " ORDER BY started_at DESC, rowid DESC LIMIT ?", (*args, limit)
        )
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        for row in rows:
            for key in ("trigger", "condition_trace", "actions"):
                row[key] = json.loads(row[key])
        return rows

    def counts(self, since: datetime) -> list[tuple[str, str, int]]:
        """Läufe je Regel und UTC-Tag seit `since` (Grundlage der Heatmap)."""
        cur = self._db.execute(
            "SELECT rule_id, substr(started_at, 1, 10), COUNT(*) FROM rule_runs"
            " WHERE started_at >= ? AND result NOT IN ('skipped', 'paused')"
            " GROUP BY rule_id, substr(started_at, 1, 10) ORDER BY 2, 1",
            (since.astimezone(UTC).isoformat(),),
        )
        return [(str(r), str(d), int(n)) for r, d, n in cur.fetchall()]

    def get_flag(self, name: str, default: bool = False) -> bool:
        row = self._db.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        return default if row is None else row[0] == "1"

    def set_flag(self, name: str, value: bool) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO flags VALUES (?, ?)", (name, "1" if value else "0")
        )
        self._db.commit()
