"""Schreibende MCP-Funktionen. Alles läuft über den RuleManager (gleiche Pipeline wie Dateien)."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..alerts import AlertStore
from ..authoring import AuthoringError, RuleManager
from ..catalog import Catalog
from ..clock import Now
from .service import ToolError

_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
MAX_DESCRIPTION = 600
MAX_SNOOZE_MINUTES = 7 * 24 * 60


@dataclass
class WriteService:
    manager: RuleManager
    alerts: AlertStore
    catalog: Catalog | None
    now: Now
    writes_per_hour: int = 30
    _stamps: deque[datetime] = field(default_factory=deque)

    def _limit(self) -> None:
        now = self.now()
        while self._stamps and now - self._stamps[0] > timedelta(hours=1):
            self._stamps.popleft()
        if len(self._stamps) >= self.writes_per_hour:
            raise ToolError(f"write rate limit reached ({self.writes_per_hour} per hour)")
        self._stamps.append(now)

    async def _run(self, call: Callable[[], Any]) -> dict[str, Any]:
        self._limit()
        try:
            result: dict[str, Any] = await call()
        except AuthoringError as exc:
            detail = f" report={exc.report}" if exc.report else ""
            raise ToolError(f"{exc}{detail}") from exc
        return result

    async def create_rule(self, rule_yaml: str, intent: str) -> dict[str, Any]:
        return await self._run(lambda: self.manager.create(rule_yaml, intent))

    async def update_rule(self, rule_id: str, rule_yaml: str, intent: str) -> dict[str, Any]:
        return await self._run(lambda: self.manager.update(rule_id, rule_yaml, intent))

    async def set_rule_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        return await self._run(lambda: self.manager.set_enabled(rule_id, enabled))

    async def delete_rule(self, rule_id: str) -> dict[str, Any]:
        return await self._run(lambda: self.manager.delete(rule_id))

    async def rollback_rule(self, rule_id: str, version: int) -> dict[str, Any]:
        return await self._run(lambda: self.manager.rollback(rule_id, version))

    async def acknowledge_alert(self, id: str, snooze_minutes: int) -> dict[str, Any]:  # noqa: A002
        if not 1 <= snooze_minutes <= MAX_SNOOZE_MINUTES:
            raise ToolError(f"snooze_minutes must be between 1 and {MAX_SNOOZE_MINUTES}")
        if id not in {a.id for a in self.alerts.alerts()}:
            raise ToolError(f"unknown or not active alert {id!r}")
        until = self.now() + timedelta(minutes=snooze_minutes)
        await self.alerts.snooze(id, until)
        return {"id": id, "snoozed_until": until.isoformat()}

    async def set_entity_description(self, entity_id: str, text: str) -> dict[str, Any]:
        if self.catalog is None:
            raise ToolError("catalog is not configured (QDRANT_URL unset)")
        if not _ENTITY.match(entity_id):
            raise ToolError(f"invalid entity_id {entity_id!r}")
        text = text.strip()
        if not text or len(text) > MAX_DESCRIPTION:
            raise ToolError(f"text must be 1..{MAX_DESCRIPTION} characters")
        self.catalog.set_description(entity_id, text)
        return {"entity_id": entity_id, "source": "curated"}
