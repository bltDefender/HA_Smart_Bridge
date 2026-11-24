"""Audit-Log: SQLite und JSON-Log. Rückgaben und Details enthalten nie Tokens oder Secrets."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .clock import Now, utc_now

log = logging.getLogger("smart_bridge.audit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    subject TEXT,
    details TEXT NOT NULL,
    result TEXT NOT NULL
)
"""
_SECRET_HINTS = ("token", "secret", "password", "authorization", "api_key")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "***" if any(h in str(k).lower() for h in _SECRET_HINTS) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, db_path: Path | str = ":memory:", now: Now = utc_now) -> None:
        self._now = now
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record(
        self,
        actor: str,
        action: str,
        subject: str | None = None,
        details: dict[str, Any] | None = None,
        result: str = "ok",
    ) -> None:
        safe = redact(details or {})
        ts = self._now().isoformat()
        self._db.execute(
            "INSERT INTO audit_log (ts, actor, action, subject, details, result) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, actor, action, subject, json.dumps(safe, default=str, ensure_ascii=False), result),
        )
        self._db.commit()
        log.info(
            "audit",
            extra={"actor": actor, "action": action, "subject": subject, "result": result},
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT ts, actor, action, subject, details, result FROM audit_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts": ts,
                "actor": a,
                "action": act,
                "subject": s,
                "details": json.loads(d),
                "result": r,
            }
            for ts, a, act, s, d, r in rows
        ]
