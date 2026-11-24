"""Quellenunabhängiges Event, das Adapter an die Engine liefern."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class BridgeEvent:
    source: Literal["ha", "mqtt", "webhook"]
    type: str  # HA-Event-Typ bzw. "mqtt_message"
    data: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[BridgeEvent], Awaitable[None]]
