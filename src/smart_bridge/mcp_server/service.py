"""Lesende MCP-Funktionen als normale async-Methoden (ohne MCP-Abhängigkeit testbar)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..alerts import AlertStore, TargetCache
from ..catalog import Catalog, CatalogUnavailableError
from ..catalog.documents import describe_condition, rule_doc, rule_entities
from ..clock import Now
from ..engine import RuleEngine
from ..history import StateHistory
from ..rules import rules_json_schema
from ..rules.models import Rule
from ..simulate import Env, backtest_rule, simulate_rule, trace_to_dict
from ..state import StateStore
from .examples import RULE_EXAMPLES

MAX_LIMIT = 50
VersionLister = Callable[[str], list[dict[str, Any]]]


class ToolError(Exception):
    """Fehler, der dem Aufrufer (Claude) als Klartext zurückgegeben wird."""


@dataclass
class ReadService:
    engine: RuleEngine
    store: StateStore
    alerts: AlertStore
    targets: Callable[[], TargetCache]
    history: StateHistory
    catalog: Catalog | None
    env: Env
    now: Now
    config_dir: Path
    max_backtest_days: int = 14
    versions: VersionLister | None = None

    # --- Suche ------------------------------------------------------------------

    def _catalog(self) -> Catalog:
        if self.catalog is None:
            raise ToolError("catalog is not configured (QDRANT_URL unset)")
        return self.catalog

    async def search_entities(
        self, query: str, domain: str | None = None, area: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        try:
            hits = await self._catalog().search_entities(
                query, domain, area, min(max(limit, 1), MAX_LIMIT)
            )
        except CatalogUnavailableError as exc:
            raise ToolError("catalog unavailable (degraded), try again later") from exc
        return [_without(h, "key", "text", "labels") | {"description": h.get("text")} for h in hits]

    async def search_rules(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            hits = await self._catalog().search_rules(query, min(max(limit, 1), MAX_LIMIT))
        except CatalogUnavailableError as exc:
            raise ToolError("catalog unavailable (degraded), try again later") from exc
        return [
            _without(h, "key", "text", "description_hash") | {"summary": h.get("text")}
            for h in hits
        ]

    # --- Zustand ------------------------------------------------------------------

    def get_state(self, entity_id: str) -> dict[str, Any]:
        st = self.store.get(entity_id)
        if st is None:
            raise ToolError(f"unknown entity {entity_id!r}; use search_entities")
        return {
            "entity_id": st.entity_id,
            "state": st.state,
            "attributes": st.attributes,
            "stale": st.stale,
            "last_changed": st.last_changed.astimezone(self.env.zone).isoformat(),
        }

    def list_alerts(self) -> list[dict[str, Any]]:
        return [a.to_json(self.env.zone) for a in self.alerts.alerts()]

    def list_model_targets(self) -> dict[str, Any]:
        cache = self.targets()
        return {
            "received": cache.received,
            "version": cache.version,
            "alert_entity": cache.alert_entity,
            "targets": list(cache.targets.values()),
        }

    # --- Regeln -------------------------------------------------------------------

    def _rule(self, rule_id: str) -> Rule:
        rules = self.engine.rules
        rule = rules.get(rule_id)
        if rule is None:
            children = [r for r in rules.values() if r.parent_id == rule_id]
            if not children:
                raise ToolError(f"unknown rule {rule_id!r}; use list_rules")
            rule = children[0]
        return rule

    def list_rules(self) -> list[dict[str, Any]]:
        out = []
        for rid, rule in sorted(self.engine.rules.items()):
            level = self.engine.alert_state(rid)
            out.append(
                {
                    "id": rid,
                    "parent": rule.parent_id,
                    "title": rule.title,
                    "intent": rule.intent,
                    "status": rule.status,
                    "enabled": rule.enabled,
                    "tags": rule.tags,
                    "created_by": rule.created_by,
                    "target": rule.alert.target if rule.alert else None,
                    "severity": rule.alert.severity if rule.alert else None,
                    "has_actions": bool(rule.actions or rule.on_activate or rule.on_clear),
                    "alert_active": level.active,
                }
            )
        return out

    def rule_yaml(self, rule_id: str) -> str:
        rule = self._rule(rule_id)
        if rule.parent_id:  # per_entity: das Original zeigen, nicht die Expansion
            rule = self._rule(rule.parent_id) if rule.parent_id in self.engine.rules else rule
        data = rule.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
        data = {"id": rule.id, **data}
        yaml = YAML()
        yaml.default_flow_style = False
        buf = StringIO()
        yaml.dump({"rules": [json.loads(json.dumps(data, default=str))]}, buf)
        return buf.getvalue()

    def explain_rule(self, rule_id: str) -> dict[str, Any]:
        rule = self._rule(rule_id)
        trace = self.engine.explain(rule.id)
        level = self.engine.alert_state(rule.id)
        lines = [f"Regel {rule.id}: {rule.title or rule.id}."]
        if rule.intent:
            lines.append(f"Ursprünglicher Wunsch: „{rule.intent}“")
        if rule.condition:
            lines.append(f"Bedingung: {describe_condition(rule.condition)}.")
        if rule.for_:
            lines.append(f"Muss mindestens {rule.for_} anhalten.")
        if rule.alert:
            lines.append(f"Meldung ({rule.alert.severity}) am Ziel {rule.alert.target}.")
        if rule.actions or rule.on_activate or rule.on_clear:
            lines.append(f"Löst Aktionen aus (Modus {rule.mode}).")
        verdict = {True: "erfüllt", False: "nicht erfüllt", None: "unbekannt"}[trace.result]
        lines.append(
            f"Aktuell: Bedingung {verdict}; Alert {'aktiv' if level.active else 'inaktiv'}."
        )
        return {
            "rule_id": rule.id,
            "text": " ".join(lines),
            "intent": rule.intent,
            "status": rule.status,
            "enabled": rule.enabled,
            "alert_active": level.active,
            "evaluation": trace_to_dict(trace),
            "entities": sorted(rule_entities(rule)),
            "catalog_text": rule_doc(rule).text,
        }

    async def simulate_rule(self, rule_yaml: str, at: str | None = None) -> dict[str, Any]:
        when = _parse_at(at, self.env) if at else None
        return await simulate_rule(
            rule_yaml, self.env, self.store.get, self.history, self.now(), when
        )

    async def backtest_rule(self, rule_yaml: str, days: int = 7) -> dict[str, Any]:
        days = min(max(days, 1), self.max_backtest_days)
        return await backtest_rule(rule_yaml, self.env, self.history, self.now(), days)

    def list_rule_versions(self, rule_id: str) -> list[dict[str, Any]]:
        self._rule(rule_id)
        return self.versions(rule_id) if self.versions else []

    # --- Resources ----------------------------------------------------------------

    def schema_json(self) -> str:
        return json.dumps(rules_json_schema(), ensure_ascii=False, indent=2)

    def examples(self) -> str:
        return RULE_EXAMPLES

    def policy_text(self) -> str:
        path = self.config_dir / "policy.yaml"
        return path.read_text(encoding="utf-8") if path.is_file() else "# no policy.yaml\n"

    def entity_resource(self, entity_id: str) -> dict[str, Any]:
        users = [
            rid for rid, r in sorted(self.engine.rules.items()) if entity_id in rule_entities(r)
        ]
        return {**self.get_state(entity_id), "used_by_rules": users}


def _without(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in keys}


def _parse_at(raw: str, env: Env) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ToolError(f"invalid 'at' (ISO-8601 expected): {raw!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=env.zone)
