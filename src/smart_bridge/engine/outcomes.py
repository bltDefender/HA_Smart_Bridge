"""Ergebnisse der RuleEngine. Executor und AlertStore (M5/M6) konsumieren sie."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..rules.models import Action
from .conditions import TraceNode

Phase = Literal["actions", "on_activate", "on_clear"]


@dataclass(frozen=True)
class TriggerInfo:
    type: str  # Trigger-Typ oder "level" (abgeleitet) bzw. "start"
    at: datetime
    index: int | None = None
    entity_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertActivated:
    rule_id: str
    since: datetime
    trace: TraceNode


@dataclass(frozen=True)
class AlertCleared:
    rule_id: str
    at: datetime
    trace: TraceNode


@dataclass(frozen=True)
class ActionsRequested:
    rule_id: str
    run_id: str
    phase: Phase
    actions: tuple[Action, ...]
    trigger: TriggerInfo
    trace: TraceNode
    shadow: bool = False


Outcome = AlertActivated | AlertCleared | ActionsRequested
OutcomeSink = Callable[[Outcome], Awaitable[None]]
