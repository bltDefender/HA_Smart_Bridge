"""Zustandslose Hilfen für das Trigger-Matching."""

from __future__ import annotations

from typing import Any

from ..rules.models import NumericStateTrigger


def topic_matches(topic_filter: str, topic: str) -> bool:
    """MQTT-Filter mit `+` und `#`."""
    f_parts, t_parts = topic_filter.split("/"), topic.split("/")
    for i, part in enumerate(f_parts):
        if part == "#":
            return True
        if i >= len(t_parts) or (part != "+" and part != t_parts[i]):
            return False
    return len(f_parts) == len(t_parts)


def has_wildcard(topic_filter: str) -> bool:
    return "+" in topic_filter or "#" in topic_filter


def data_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Alle erwarteten Schlüssel müssen (rekursiv) übereinstimmen."""
    for key, want in expected.items():
        if key not in actual:
            return False
        got = actual[key]
        if isinstance(want, dict):
            if not isinstance(got, dict) or not data_matches(want, got):
                return False
        elif got != want:
            return False
    return True


def numeric_satisfied(trig: NumericStateTrigger, value: float) -> bool:
    return (trig.above is None or value > trig.above) and (trig.below is None or value < trig.below)


def numeric_rearmed(trig: NumericStateTrigger, value: float) -> bool:
    """Wert liegt außerhalb der Schwelle, inklusive Hysterese-Abstand."""
    h = trig.hysteresis or 0.0
    if not numeric_satisfied(trig, value):
        below_range = trig.above is not None and value <= trig.above - h
        above_range = trig.below is not None and value >= trig.below + h
        return h == 0.0 or below_range or above_range
    return False
