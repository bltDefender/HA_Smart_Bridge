"""`haus3d_targets`-Handshake: Ziele des 3D-Modells cachen und Regel-Ziele dagegen prüfen."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ..events import BridgeEvent
from ..rules.models import Rule

log = logging.getLogger(__name__)

FireEvent = Callable[[str, dict[str, Any]], Awaitable[Any]]
EntityExists = Callable[[str], bool]


class TargetCache:
    def __init__(self, fire_event: FireEvent | None = None) -> None:
        self._fire = fire_event
        self.targets: dict[str, dict[str, Any]] = {}
        self.alert_entity: str | None = None
        self.version: Any = None
        self.received = False

    async def request(self) -> None:
        """Sendet `haus3d_command: list_targets`; die Antwort kommt als Event."""
        if self._fire is None:
            return
        try:
            await self._fire("haus3d_command", {"action": "list_targets"})
        except Exception as exc:  # Modell evtl. nicht geladen: nur Warnung
            log.warning("list_targets failed: %r", exc)

    def on_event(self, event: BridgeEvent) -> bool:
        """True, wenn das Event den Cache aktualisiert hat."""
        if event.type != "haus3d_targets":
            return False
        raw = event.data.get("targets")
        self.targets = {
            str(t["target"]): t for t in raw or [] if isinstance(t, dict) and "target" in t
        }
        self.alert_entity = event.data.get("alert_entity")
        self.version = event.data.get("version")
        self.received = True
        return True

    def is_valid(self, target: str, entity_exists: EntityExists) -> bool:
        return target in self.targets or entity_exists(target)

    def validate(self, rules: Iterable[Rule], entity_exists: EntityExists) -> dict[str, str]:
        """`rule_id → Warnung` für unbekannte Ziele. Nur Warnung, nie Abbruch."""
        if not self.received:
            return {}
        warnings: dict[str, str] = {}
        for rule in rules:
            target = rule.alert.target if rule.alert else None
            if target and "{{" not in target and not self.is_valid(target, entity_exists):
                warnings[rule.id] = f"alert target {target!r} is unknown to the 3D model"
        return warnings
