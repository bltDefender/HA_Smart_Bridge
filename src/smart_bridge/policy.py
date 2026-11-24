"""Risikostufen (`config/policy.yaml`). Die Stufe folgt immer dem Inhalt einer Regel, nie dem Pfad.

Stufe 0: nur Alerts. Stufe 1: Aktionen auf unkritischen Diensten (Allowlist).
Stufe 2: alles andere, auch Unbekanntes (fail-safe) → `shadow`, bis außerhalb von MCP/UI
freigegeben.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .rules.models import Action, Rule


def _globs(raw: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError("policy: glob lists must be lists of strings")
    return tuple(raw)


def _match(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(value, p) for p in patterns)


@dataclass(frozen=True)
class Policy:
    max_fires_per_day: float = 20
    backtest_days: int = 7
    max_activations_per_hour: int = 30
    writes_per_hour: int = 30
    tier1_services: tuple[str, ...] = ("light.*", "switch.*", "notify.*", "input_*.*")
    tier1_deny_services: tuple[str, ...] = ()
    tier1_deny_entities: tuple[str, ...] = (
        "switch.*wallbox*",
        "switch.*garage*",
        "switch.*sg_ready*",
    )
    tier1_tools: tuple[str, ...] = ()
    mqtt_allowed_topics: tuple[str, ...] = ()
    webhook_allowed_urls: tuple[str, ...] = ()
    tier2_services: tuple[str, ...] = (
        "lock.*",
        "alarm_control_panel.*",
        "climate.*",
        "valve.*",
        "cover.*garage*",
    )
    tier2_entities: tuple[str, ...] = (
        "lock.*",
        "alarm_control_panel.*",
        "climate.*",
        "valve.*",
        "*garage*",
        "*wallbox*",
        "*waermepumpe*",
        "*sg_ready*",
    )
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def load(cls, path: Path) -> Policy:
        if not path.is_file():
            return cls()
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("policy.yaml must be a mapping")
        limits = data.get("limits") or {}
        t1, t2 = data.get("tier1") or {}, data.get("tier2") or {}
        d = cls()
        return cls(
            max_fires_per_day=float(limits.get("max_fires_per_day", d.max_fires_per_day)),
            backtest_days=int(limits.get("backtest_days", d.backtest_days)),
            max_activations_per_hour=int(
                limits.get("max_activations_per_hour", d.max_activations_per_hour)
            ),
            writes_per_hour=int(limits.get("writes_per_hour", d.writes_per_hour)),
            tier1_services=_globs(t1.get("services"), d.tier1_services),
            tier1_deny_services=_globs(t1.get("deny_services"), d.tier1_deny_services),
            tier1_deny_entities=_globs(t1.get("deny_entities"), d.tier1_deny_entities),
            tier1_tools=_globs(t1.get("tools"), d.tier1_tools),
            mqtt_allowed_topics=_globs(t1.get("mqtt_topics"), d.mqtt_allowed_topics),
            webhook_allowed_urls=_globs(t1.get("webhook_urls"), d.webhook_allowed_urls),
            tier2_services=_globs(t2.get("services"), d.tier2_services),
            tier2_entities=_globs(t2.get("entities"), d.tier2_entities),
            raw=data,
        )

    # --- Stufe bestimmen -------------------------------------------------------

    def tier_of(self, rule: Rule) -> int:
        bound = list(rule.per_entity or [])
        actions = [*(rule.actions or []), *(rule.on_activate or []), *(rule.on_clear or [])]
        return max((self._action_tier(a, bound) for a in actions), default=0)

    def _action_tier(self, action: Action, bound: list[str]) -> int:
        extra = action.model_extra or {}
        kind = action.type
        if kind in ("delay", "wait_for"):
            return 0
        if kind == "choose":
            nested = [a for c in extra["choices"] for a in c["then"]]
            nested += extra.get("default") or []
            return max(
                (self._action_tier(Action.model_validate(a), bound) for a in nested), default=0
            )
        if kind == "if":
            nested = [*extra["then"], *(extra.get("else") or [])]
            return max(
                (self._action_tier(Action.model_validate(a), bound) for a in nested), default=0
            )
        found = _entity_ids(extra)
        if any("{{" in e for e in found):
            found |= set(bound)  # per_entity: das Template steht für alle gebundenen Entities
        entities = sorted(found)
        if _any_match(entities, self.tier2_entities):
            return 2
        if kind == "mqtt_publish":
            return 1 if _match(str(extra.get("topic", "")), self.mqtt_allowed_topics) else 2
        if kind == "webhook":
            return 1 if _match(str(extra.get("url", "")), self.webhook_allowed_urls) else 2
        if kind == "ha_tool":
            return 1 if _match(str(extra.get("tool", "")), self.tier1_tools) else 2
        service = str(extra.get("service") or ("notify.notify" if kind == "notify" else ""))
        if not service or _match(service, self.tier2_services):
            return 2
        if _match(service, self.tier1_deny_services) or _any_match(
            entities, self.tier1_deny_entities
        ):
            return 2
        return 1 if _match(service, self.tier1_services) else 2


def _any_match(values: Iterable[str], patterns: Iterable[str]) -> bool:
    pats = tuple(patterns)
    return any(_match(v, pats) for v in values)


def _entity_ids(value: Any) -> set[str]:
    """Alle `entity_id`-Werte (auch verschachtelt, auch Listen) einer Aktion."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "entity_id":
                found.update([item] if isinstance(item, str) else _strings(item))
            else:
                found |= _entity_ids(item)
    elif isinstance(value, list):
        for item in value:
            found |= _entity_ids(item)
    return found


def _strings(item: Any) -> list[str]:
    return [i for i in item if isinstance(i, str)] if isinstance(item, list) else []
