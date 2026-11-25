"""Klartext für Trigger und Aktionen (Beschriftung der Graph-Knoten)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from io import StringIO
from typing import Any

from ruamel.yaml import YAML

from ..catalog.documents import describe_condition
from ..rules.models import (
    ActiveWindow,
    Condition,
    CronTrigger,
    DateTrigger,
    EventTrigger,
    IntervalTrigger,
    MqttTrigger,
    NumericStateTrigger,
    Rule,
    StartupTrigger,
    StateTrigger,
    SunTrigger,
    TimePatternTrigger,
    TimeTrigger,
    WebhookTrigger,
)

SUN_EVENTS = {
    "sunrise": "Sonnenaufgang",
    "sunset": "Sonnenuntergang",
    "civil_dawn": "Bürgerliche Morgendämmerung",
    "civil_dusk": "Bürgerliche Abenddämmerung",
    "nautical_dawn": "Nautische Morgendämmerung",
    "nautical_dusk": "Nautische Abenddämmerung",
    "astronomical_dawn": "Astronomische Morgendämmerung",
    "astronomical_dusk": "Astronomische Abenddämmerung",
}
WEEKDAYS = {
    "mon": "Mo",
    "tue": "Di",
    "wed": "Mi",
    "thu": "Do",
    "fri": "Fr",
    "sat": "Sa",
    "sun": "So",
}


def offset_text(offset: str) -> str:
    """`-00:15:00` → `− 15 min`; leer bei 0."""
    sign = "−" if offset.startswith("-") else "+"
    h, m, s = (int(p) for p in offset.lstrip("-").split(":"))
    parts = [f"{h} h" if h else "", f"{m} min" if m else "", f"{s} s" if s else ""]
    text = " ".join(p for p in parts if p)
    return f"{sign} {text}" if text else ""


def duration_text(value: str) -> str:
    return offset_text(value).lstrip("+ ").strip() or "0 s"


def _days(days: Sequence[str] | None) -> str:
    return f" ({', '.join(WEEKDAYS[d] for d in days)})" if days else ""


def describe_trigger(trig: Any) -> str:
    if isinstance(trig, StateTrigger):
        text = f"{trig.entity_id}"
        if trig.from_ is not None:
            text += f" von {trig.from_}"
        text += f" → {trig.to}" if trig.to is not None else " ändert sich"
        return text + (f" für {duration_text(trig.for_)}" if trig.for_ else "")
    if isinstance(trig, NumericStateTrigger):
        bounds = [f"über {trig.above:g}" if trig.above is not None else ""]
        bounds.append(f"unter {trig.below:g}" if trig.below is not None else "")
        text = f"{trig.entity_id} {' / '.join(b for b in bounds if b)}"
        text += f" (Hysterese {trig.hysteresis:g})" if trig.hysteresis else ""
        return text + (f" für {duration_text(trig.for_)}" if trig.for_ else "")
    if isinstance(trig, EventTrigger):
        return f"Event {trig.event_type}" + (
            f" {json.dumps(trig.event_data)}" if trig.event_data else ""
        )
    if isinstance(trig, MqttTrigger):
        return f"MQTT {trig.topic}" + (f" = {trig.payload}" if trig.payload is not None else "")
    if isinstance(trig, WebhookTrigger):
        return f"Webhook {trig.webhook_id}"
    if isinstance(trig, StartupTrigger):
        return "Start der Bridge"
    if isinstance(trig, TimeTrigger):
        return f"Um {trig.at} Uhr{_days(trig.weekdays)}"
    if isinstance(trig, TimePatternTrigger):
        fields = [f"{k}={v}" for k in ("hours", "minutes", "seconds") if (v := getattr(trig, k))]
        return "Zeitmuster " + ", ".join(fields)
    if isinstance(trig, CronTrigger):
        return f"Cron {trig.expression}"
    if isinstance(trig, IntervalTrigger):
        return f"Alle {duration_text(trig.every)}"
    if isinstance(trig, DateTrigger):
        if trig.on:
            return f"Am {trig.on}{' (jährlich)' if trig.yearly else ''} um {trig.at}"
        return f"Vom {trig.from_} bis {trig.to}{' (jährlich)' if trig.yearly else ''} um {trig.at}"
    if isinstance(trig, SunTrigger):
        off = offset_text(trig.offset)
        return f"{SUN_EVENTS[trig.event]}{f' {off}' if off else ''}"
    return str(getattr(trig, "type", "?"))


def trigger_entity(trig: Any) -> str | None:
    eid = getattr(trig, "entity_id", None)
    return eid if isinstance(eid, str) else None


def describe_window(window: ActiveWindow) -> str:
    parts = []
    if window.after or window.before:
        parts.append(f"{window.after or '00:00'}–{window.before or '24:00'} Uhr")
    if window.weekdays:
        parts.append(_days(window.weekdays).strip(" ()"))
    if window.sun:
        sun = window.sun
        if sun.after:
            parts.append(
                f"nach {SUN_EVENTS[sun.after]} {offset_text(sun.after_offset or '')}".strip()
            )
        if sun.before:
            parts.append(
                f"vor {SUN_EVENTS[sun.before]} {offset_text(sun.before_offset or '')}".strip()
            )
    return "Aktiv: " + ", ".join(parts)


def describe_leaf(cond: Condition) -> str:
    return describe_condition(cond)


def describe_action(type_: str, extra: dict[str, Any]) -> str:
    target = extra.get("target")
    target_text = ""
    if isinstance(target, dict) and target.get("entity_id"):
        ids = target["entity_id"]
        target_text = " → " + (ids if isinstance(ids, str) else ", ".join(map(str, ids)))
    if type_ in ("ha_service", "notify"):
        return f"{extra.get('service', 'notify.notify')}{target_text}"
    if type_ == "ha_tool":
        return f"MCP-Tool {extra['tool']}"
    if type_ == "mqtt_publish":
        return f"MQTT {extra['topic']}"
    if type_ == "webhook":
        return f"Webhook {extra.get('method', 'POST')} {extra['url']}"
    if type_ == "delay":
        return f"Warte {duration_text(str(extra['duration']))}"
    if type_ == "wait_for":
        return f"Warte auf: {describe_condition(Condition.model_validate(extra['condition']))}"
    if type_ == "if":
        return f"Wenn {describe_condition(Condition.model_validate(extra['condition']))}"
    return "Fallunterscheidung"


def rule_to_yaml(rule: Rule) -> str:
    data = rule.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
    yaml = YAML()
    yaml.default_flow_style = False
    buf = StringIO()
    yaml.dump({"rules": [json.loads(json.dumps({"id": rule.id, **data}, default=str))]}, buf)
    return buf.getvalue()
