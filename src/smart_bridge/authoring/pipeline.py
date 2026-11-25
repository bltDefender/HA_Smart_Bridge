"""Validierungs-Pipeline für jede Regeländerung (MCP und Datei). Atomar: bei Fehler passiert nichts.

1. Schema + Pydantic  2. Entity-Existenz  3. Ziel-IDs (Warnung)  4. Dubletten/Konflikte
5. Pflicht-Backtest bei Aktionen  6. Risikostufe  (+ Schleifenschutz über alle Regeln)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..alerts import TargetCache
from ..catalog import CatalogUnavailableError
from ..catalog.documents import rule_entities
from ..policy import Policy, _entity_ids
from ..rules import parse_rules_text
from ..rules.models import Rule

Backtest = Callable[[str, int], Awaitable[dict[str, Any]]]
Search = Callable[[str], Awaitable[list[dict[str, Any]]]]

_OPPOSITES = {
    ("turn_on", "turn_off"),
    ("lock", "unlock"),
    ("open_cover", "close_cover"),
    ("open_valve", "close_valve"),
    ("alarm_arm_away", "alarm_disarm"),
}
_OPPOSITE = {a: b for a, b in _OPPOSITES} | {b: a for a, b in _OPPOSITES}
DUPLICATE_SCORE = 0.92


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    backtest: dict[str, Any] | None = None
    tier: int = 0
    rules: list[Rule] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "conflicts": self.conflicts,
            "duplicates": self.duplicates,
            "backtest": self.backtest,
            "tier": self.tier,
        }


def action_effects(rule: Rule) -> set[tuple[str, str]]:
    """`(entity_id, verb)` aller `ha_service`-Aktionen (Templates über `per_entity` aufgelöst)."""
    out: set[tuple[str, str]] = set()
    for action in _flat_actions(rule):
        extra = action.model_extra or {}
        if action.type != "ha_service":
            continue
        verb = str(extra.get("service", "")).split(".")[-1]
        for eid in _bound(_entity_ids(extra), rule):
            out.add((eid, verb))
    return out


def _flat_actions(rule: Rule) -> list[Any]:
    from ..rules.models import Action

    def walk(actions: list[Any]) -> list[Any]:
        found: list[Any] = []
        for a in actions:
            found.append(a)
            extra = a.model_extra or {}
            nested: list[Any] = []
            if a.type == "choose":
                nested = [x for c in extra["choices"] for x in c["then"]] + (
                    extra.get("default") or []
                )
            elif a.type == "if":
                nested = [*extra["then"], *(extra.get("else") or [])]
            found += walk([Action.model_validate(x) for x in nested])
        return found

    return walk([*(rule.actions or []), *(rule.on_activate or []), *(rule.on_clear or [])])


def _bound(entities: set[str], rule: Rule) -> set[str]:
    out = {e for e in entities if "{{" not in e}
    if any("{{" in e for e in entities):
        out |= set(rule.per_entity or [])
    return out


def trigger_entities(rule: Rule) -> set[str]:
    """Entities, deren Wechsel die Regel auslösen (explizite Trigger oder Condition-Wächter)."""
    found: set[str] = set(rule.per_entity or [])
    if rule.triggers:
        for trig in rule.triggers:
            eid = getattr(trig, "entity_id", None)
            if isinstance(eid, str) and "{{" not in eid:
                found.add(eid)
    elif rule.condition:
        found |= {e for e in rule.condition.entity_ids(rule.bindings) if "{{" not in e}
    return found


def find_cycles(rules: Mapping[str, Rule]) -> list[list[str]]:
    """Zyklen aus mindestens zwei Regeln (A schaltet, was B auslöst, …). Selbstschleifen: nein."""
    effects = {rid: {e for e, _ in action_effects(r)} for rid, r in rules.items()}
    triggers = {rid: trigger_entities(r) for rid, r in rules.items()}
    graph = {
        a: sorted(b for b in rules if b != a and effects[a] & triggers[b]) for a in sorted(rules)
    }
    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in graph[node]:
            if state.get(nxt, 0) == 0:
                visit(nxt)
            elif state[nxt] == 1:
                cycles.append([*stack[stack.index(nxt) :], nxt])
        stack.pop()
        state[node] = 2

    for node in graph:
        if state.get(node, 0) == 0:
            visit(node)
    return cycles


class Pipeline:
    def __init__(
        self,
        policy: Policy,
        *,
        entity_exists: Callable[[str], bool],
        suggest: Callable[[str], Awaitable[list[str]]] | None = None,
        targets: Callable[[], TargetCache] | None = None,
        search_rules: Search | None = None,
        backtest: Backtest | None = None,
    ) -> None:
        self.policy = policy
        self._exists, self._suggest, self._targets = entity_exists, suggest, targets
        self._search, self._backtest = search_rules, backtest

    def tier(self, rule: Rule) -> int:
        return self.policy.tier_of(rule)

    async def check(
        self,
        text: str,
        others: Mapping[str, Rule],
        *,
        full: bool = True,
        single: bool = False,
    ) -> Report:
        """`others`: alle übrigen aktiven Regeln (ohne die hier geprüften). `full=False` überspringt
        Entity-Prüfung, Dubletten und Backtest (Start: HA ist noch nicht synchronisiert)."""
        report = Report()
        rules, errors = parse_rules_text(text)
        report.errors += errors
        if errors:
            return report
        if not rules:
            report.errors.append("no rules in document")
            return report
        if single and len(rules) != 1:
            report.errors.append(f"exactly one rule expected, got {len(rules)}")
            return report
        ids = [r.id for r in rules]
        for rid in {i for i in ids if ids.count(i) > 1}:
            report.errors.append(f"duplicate rule id {rid!r} in document")
        report.rules = rules
        for rule in rules:
            if rule.status in ("shadow", "error"):
                report.errors.append(
                    f"{rule.id}: 'status: {rule.status}' is computed by the bridge, not writable"
                )
        report.tier = max(self.tier(r) for r in rules)
        if report.errors:
            return report

        merged = {**others, **{r.id: r for r in rules}}
        for cycle in find_cycles(merged):
            if set(cycle) & set(ids):
                report.errors.append("action loop: " + " -> ".join(cycle))
        if not full:
            return report

        for rule in rules:
            await self._entities(rule, report)
            self._model_targets(rule, report)
            await self._duplicates(rule, report)
            self._conflicts(rule, others, report)
        if not report.errors:
            await self._run_backtest(text, rules, report)
        return report

    async def _entities(self, rule: Rule, report: Report) -> None:
        target = rule.alert.target if rule.alert else None
        wanted = rule_entities(rule) | set(rule.per_entity or [])
        for eid in sorted(wanted - ({target} if target else set())):
            if self._exists(eid):
                continue
            hint = ""
            if self._suggest:
                try:
                    found = await self._suggest(eid)
                except CatalogUnavailableError:
                    found = []
                if found:
                    hint = f" (did you mean: {', '.join(found)}?)"
            report.errors.append(f"{rule.id}: unknown entity {eid!r}{hint}")

    def _model_targets(self, rule: Rule, report: Report) -> None:
        if self._targets is None:
            return
        for message in self._targets().validate([rule], self._exists).values():
            report.warnings.append(f"{rule.id}: {message}")

    async def _duplicates(self, rule: Rule, report: Report) -> None:
        if self._search is None:
            return
        query = " ".join(filter(None, [rule.title, rule.intent])) or rule.id
        try:
            hits = await self._search(query)
        except CatalogUnavailableError:
            report.warnings.append("catalog unavailable: duplicate check skipped")
            return
        for hit in hits:
            other = hit.get("rule_id")
            if other and other != rule.id and float(hit.get("score", 0)) >= DUPLICATE_SCORE:
                report.duplicates.append(
                    {"rule_id": other, "score": round(float(hit["score"]), 3), "for": rule.id}
                )
                report.warnings.append(f"{rule.id}: similar to existing rule {other!r}")

    def _conflicts(self, rule: Rule, others: Mapping[str, Rule], report: Report) -> None:
        mine = action_effects(rule)
        if not mine:
            return
        blocking = self.tier(rule) == 2
        for rid, other in sorted(others.items()):
            if rid == rule.id or (other.parent_id and other.parent_id == rule.id):
                continue
            clash = sorted(
                {
                    eid
                    for eid, verb in mine
                    for oid, overb in action_effects(other)
                    if eid == oid and _OPPOSITE.get(verb) == overb
                }
            )
            if clash:
                report.conflicts.append({"rule_id": rid, "entities": clash, "for": rule.id})
                message = f"{rule.id}: opposite actions with rule {rid!r} on {clash}"
                (report.errors if blocking else report.warnings).append(message)

    async def _run_backtest(self, text: str, rules: list[Rule], report: Report) -> None:
        if not any(r.actions or r.on_activate or r.on_clear for r in rules):
            return
        if self._backtest is None:
            report.warnings.append("backtest unavailable")
            return
        result = await self._backtest(text, self.policy.backtest_days)
        report.backtest = result
        if not result.get("valid", False):
            report.errors += [f"backtest: {e}" for e in result.get("errors", [])]
            return
        limit = self.policy.max_fires_per_day
        per_parent: dict[str, float] = {}
        parents = {c.id: (c.parent_id or c.id) for r in rules for c in _children(r)}
        for rid, entry in result.get("rules", {}).items():
            key = parents.get(rid, rid)
            per_parent[key] = per_parent.get(key, 0) + float(entry.get("fires_per_day", 0))
        for rid, rate in sorted(per_parent.items()):
            if rate > limit:
                report.errors.append(
                    f"{rid}: would fire {rate:g} times per day in the backtest "
                    f"(limit {limit:g}); narrow the trigger/condition or add debounce/for"
                )


def _children(rule: Rule) -> list[Rule]:
    from ..rules.expand import expand_per_entity

    try:
        return expand_per_entity(rule)
    except ValueError:
        return [rule]
