"""Deterministische Dokumente für den Katalog: Entities aus der HA-Registry, Regeln aus der YAML."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..rules.models import Action, Condition, Rule
from ..state import EntityState

_DOMAIN_NAMES = {
    "switch": "Schalter",
    "light": "Licht",
    "lock": "Schloss",
    "cover": "Rollo/Tor",
    "binary_sensor": "Binärsensor",
    "sensor": "Sensor",
    "climate": "Klima/Heizung",
    "input_boolean": "Schalter-Helfer",
    "button": "Taster",
    "number": "Zahlenwert",
    "select": "Auswahl",
}
_DOMAIN_STATES = {
    "switch": ["on", "off"],
    "light": ["on", "off"],
    "lock": ["locked", "unlocked", "locking", "unlocking", "jammed", "open"],
    "cover": ["open", "closed", "opening", "closing"],
    "binary_sensor": ["on", "off"],
    "input_boolean": ["on", "off"],
    "climate": ["off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"],
}
DOMAINS = tuple(_DOMAIN_NAMES)


def description_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Registry:
    """Ausschnitt der HA-Registries (`config/*_registry/list`)."""

    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    areas: dict[str, str] = field(default_factory=dict)  # area_id -> Name

    @classmethod
    def from_lists(
        cls,
        entities: list[dict[str, Any]],
        devices: list[dict[str, Any]],
        areas: list[dict[str, Any]],
    ) -> Registry:
        return cls(
            {e["entity_id"]: e for e in entities},
            {d["id"]: d for d in devices},
            {a["area_id"]: str(a.get("name", a["area_id"])) for a in areas},
        )

    def area_of(self, entity_id: str) -> str | None:
        ent = self.entities.get(entity_id, {})
        area_id = ent.get("area_id") or self.devices.get(ent.get("device_id") or "", {}).get(
            "area_id"
        )
        return self.areas.get(area_id) if area_id else None


@dataclass(frozen=True)
class Doc:
    key: str  # entity_id bzw. rule_id
    text: str
    payload: dict[str, Any]


def entity_doc(
    entity_id: str,
    registry: Registry,
    state: EntityState | None,
    model_target: str | None = None,
    curated: str | None = None,
) -> Doc:
    ent = registry.entities.get(entity_id, {})
    device = registry.devices.get(ent.get("device_id") or "", {})
    attrs = state.attributes if state else {}
    domain = entity_id.split(".", 1)[0]
    name = ent.get("name") or ent.get("original_name") or attrs.get("friendly_name") or entity_id
    area = registry.area_of(entity_id)
    device_class = ent.get("device_class") or ent.get("original_device_class")
    device_class = device_class or attrs.get("device_class")
    unit = attrs.get("unit_of_measurement")
    options = attrs.get("options") or _DOMAIN_STATES.get(domain)

    parts = [f"{name} ({_DOMAIN_NAMES.get(domain, domain)}, {entity_id})."]
    if area:
        parts.append(f"Bereich: {area}.")
    if device:
        label = device.get("name_by_user") or device.get("name")
        maker = " ".join(str(x) for x in (device.get("manufacturer"), device.get("model")) if x)
        parts.append(f"Gerät: {label}{f' ({maker})' if maker else ''}.")
    if device_class:
        parts.append(f"Geräteklasse: {device_class}.")
    if unit:
        parts.append(f"Einheit: {unit}.")
    if options:
        parts.append(f"Zustände: {', '.join(str(o) for o in options)}.")
    if model_target:
        parts.append(f"Ziel im 3D-Modell: {model_target}.")
    generated = " ".join(parts)
    text = f"{generated} {curated}" if curated else generated
    return Doc(
        entity_id,
        text,
        {
            "entity_id": entity_id,
            "domain": domain,
            "area": area,
            "device_id": ent.get("device_id"),
            "device_class": device_class,
            "model_target": model_target,
            "labels": list(ent.get("labels") or []),
            "description_hash": description_hash(text),
            "source": "curated" if curated else "generated",
            "text": text,
        },
    )


# --- Regeln -------------------------------------------------------------------


def describe_condition(cond: Condition) -> str:
    if cond.state is not None:
        is_ = cond.state.is_ if isinstance(cond.state.is_, str) else " oder ".join(cond.state.is_)
        return f"{cond.state.entity_id} ist {is_}"
    if cond.numeric_state is not None:
        n = cond.numeric_state
        bounds = [
            f"über {n.above}" if n.above is not None else "",
            f"unter {n.below}" if n.below is not None else "",
        ]
        return f"{n.entity_id} {' und '.join(b for b in bounds if b)}"
    if cond.template is not None:
        return f"Template {cond.template.value_template}"
    if cond.time is not None:
        return f"Uhrzeit {cond.time.after or ''}–{cond.time.before or ''}"
    if cond.sun is not None:
        return f"Sonne nach {cond.sun.after} / vor {cond.sun.before}"
    if cond.workday is not None:
        return "Werktag" if cond.workday else "kein Werktag"
    if cond.and_ is not None:
        return "(" + " UND ".join(describe_condition(c) for c in cond.and_) + ")"
    if cond.or_ is not None:
        return "(" + " ODER ".join(describe_condition(c) for c in cond.or_) + ")"
    assert cond.not_ is not None
    return f"NICHT {describe_condition(cond.not_)}"


def _action_entities(value: Any, out: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "entity_id":
                out.update(
                    [item] if isinstance(item, str) else [i for i in item if isinstance(i, str)]
                )
            else:
                _action_entities(item, out)
    elif isinstance(value, list):
        for item in value:
            _action_entities(item, out)


def _all_actions(rule: Rule) -> list[Action]:
    return [*(rule.actions or []), *(rule.on_activate or []), *(rule.on_clear or [])]


def rule_entities(rule: Rule) -> set[str]:
    """Alle Entities, die eine Regel liest, auslöst oder schaltet."""
    found: set[str] = set()
    if rule.condition:
        found |= rule.condition.entity_ids(rule.bindings)
    for trig in rule.triggers or []:
        eid = getattr(trig, "entity_id", None)
        if isinstance(eid, str):
            found.add(eid)
    for action in _all_actions(rule):
        _action_entities(action.model_dump(by_alias=True), found)
    if rule.alert and "." in rule.alert.target and "{{" not in rule.alert.target:
        found.add(rule.alert.target)
    return {e for e in found if "{{" not in e}


def _describe_trigger(trig: Any) -> str:
    data = trig.model_dump(by_alias=True, exclude_none=True)
    kind = data.pop("type")
    return f"{kind} " + ", ".join(f"{k}={v}" for k, v in sorted(data.items()))


def rule_doc(rule: Rule, file_hash: str | None = None) -> Doc:
    parts = [f"Regel {rule.title or rule.id}."]
    if rule.intent:
        parts.append(f"Wunsch: {rule.intent}")
    if rule.triggers:
        parts.append("Auslöser: " + "; ".join(_describe_trigger(t) for t in rule.triggers) + ".")
    if rule.condition:
        parts.append(f"Bedingung: {describe_condition(rule.condition)}.")
    if rule.alert:
        parts.append(f"Meldung ({rule.alert.severity}) am Ziel {rule.alert.target}.")
    acts = [
        f"{a.type} {json.dumps(a.model_extra or {}, ensure_ascii=False, sort_keys=True)}"
        for a in _all_actions(rule)
    ]
    if acts:
        parts.append("Aktionen: " + "; ".join(acts) + ".")
    text = " ".join(parts)
    return Doc(
        rule.id,
        text,
        {
            "rule_id": rule.id,
            "entity_ids": sorted(rule_entities(rule)),
            "target": rule.alert.target if rule.alert else None,
            "severity": rule.alert.severity if rule.alert else None,
            "file_hash": file_hash,
            "description_hash": description_hash(f"{text}|{file_hash}"),
            "tags": rule.tags,
            "text": text,
        },
    )
