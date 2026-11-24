"""RuleGraph-Builder: YAML-Regel → `{nodes, edges, layout_hints}`.

Reine Funktion ohne I/O; das Frontend rendert nur. Spalten von links nach rechts:
trigger → condition → entity → result (Alert/Aktionen). Alle Texte kommen von hier.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..catalog.documents import _action_entities
from ..rules.models import Action, Condition, Rule
from .text import (
    describe_action,
    describe_leaf,
    describe_trigger,
    describe_window,
    duration_text,
    trigger_entity,
)

EntityLookup = Callable[[str], dict[str, Any] | None]

COLUMNS = ["trigger", "condition", "entity", "result"]
PHASES = {
    "actions": "Aktionen",
    "on_activate": "Bei Aktivierung",
    "on_clear": "Bei Entwarnung",
}
LAYOUT_HINTS: dict[str, Any] = {
    "engine": "elk",
    "direction": "RIGHT",
    "columns": COLUMNS,
    "elk": {
        "elk.algorithm": "layered",
        "elk.direction": "RIGHT",
        "elk.layered.spacing.nodeNodeBetweenLayers": "60",
        "elk.spacing.nodeNode": "28",
    },
}
_COLUMN_OF = {
    "trigger": 0,
    "window": 1,
    "condition": 1,
    "for": 1,
    "entity": 2,
    "alert": 3,
    "action_group": 3,
    "action": 3,
}
_FLOW_RUN_STATUS = {"ok": "ok", "error": "error", "shadow": "shadow", "dry_run": "dry_run"}


class _Builder:
    def __init__(self, rule: Rule, entity: EntityLookup) -> None:
        self.rule, self.entity_lookup = rule, entity
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []

    # --- Primitive ----------------------------------------------------------

    def node(self, node_id: str, kind: str, type_: str, label: str, **data: Any) -> str:
        self.nodes[node_id] = {
            "id": node_id,
            "kind": kind,
            "type": type_,
            "label": label,
            "column": _COLUMN_OF[kind],
            "data": data,
        }
        return node_id

    def edge(self, source: str, target: str, kind: str = "flow", label: str | None = None) -> None:
        edge_id = f"{source}->{target}"
        if any(e["id"] == edge_id for e in self.edges):
            return
        self.edges.append(
            {"id": edge_id, "source": source, "target": target, "kind": kind, "label": label}
        )

    def uses(self, source: str, entity_id: str, label: str | None = None) -> None:
        if "{{" in entity_id:
            return
        node_id = f"entity:{entity_id}"
        if node_id not in self.nodes:
            info = self.entity_lookup(entity_id) or {}
            self.node(
                node_id,
                "entity",
                entity_id.split(".", 1)[0],
                info.get("name") or entity_id,
                entity_id=entity_id,
                domain=entity_id.split(".", 1)[0],
                area=info.get("area"),
                state=info.get("state"),
                stale=info.get("stale", False),
                known=bool(info),
            )
        self.edge(source, node_id, "uses", label)

    # --- Aufbau -----------------------------------------------------------

    def build(self) -> None:
        rule = self.rule
        heads = self._triggers()
        tails = heads
        if rule.active is not None:
            win = self.node("window", "window", "active", describe_window(rule.active))
            self._link(tails, win)
            tails = [win]
        if rule.condition is not None:
            entry, exit_ = self._condition(rule.condition, "c")
            self._link(tails, entry)
            tails = [exit_]
            if rule.for_:
                hold = self.node(
                    "for", "for", "for", f"Mindestens {duration_text(rule.for_)}", for_=rule.for_
                )
                self._link(tails, hold)
                tails = [hold]
        self._results(tails)

    def _link(self, sources: list[str], target: str) -> None:
        for source in sources:
            self.edge(source, target)

    def _triggers(self) -> list[str]:
        rule = self.rule
        if not rule.triggers:
            ids = sorted(rule.condition.entity_ids(rule.bindings)) if rule.condition else []
            label = "Änderung von: " + ", ".join(ids) if ids else "Zustandsänderung"
            node = self.node("trigger:derived", "trigger", "derived", label, derived=True)
            for eid in ids:
                self.uses(node, eid)
            return [node]
        out = []
        for i, trig in enumerate(rule.triggers):
            node = self.node(f"trigger:{i}", "trigger", trig.type, describe_trigger(trig), index=i)
            if (watched := trigger_entity(trig)) is not None:
                self.uses(node, watched)
            out.append(node)
        return out

    def _condition(self, cond: Condition, path: str) -> tuple[str, str]:
        groups = (("and", cond.and_), ("or", cond.or_), ("not", [cond.not_] if cond.not_ else None))
        for op, children in groups:
            if children is None:
                continue
            split = self.node(f"cond:{path}", "condition", op, op.upper(), op=op, path=path)
            merge = self.node(f"cond:{path}:merge", "condition", "merge", op.upper(), op=op)
            for i, child in enumerate(children):
                entry, exit_ = self._condition(child, f"{path}.{i}")
                self.edge(split, entry, "branch")
                self.edge(exit_, merge, "branch")
            return split, merge
        kind = next(
            k
            for k in ("state", "numeric_state", "template", "time", "sun", "workday")
            if getattr(cond, k) is not None
        )
        node = self.node(f"cond:{path}", "condition", kind, describe_leaf(cond), path=path)
        for eid in sorted(cond.entity_ids(self.rule.bindings)):
            self.uses(node, eid)
        return node, node

    def _results(self, tails: list[str]) -> None:
        rule = self.rule
        alert_node: str | None = None
        if rule.alert is not None:
            a = rule.alert
            alert_node = self.node(
                "alert",
                "alert",
                a.severity,
                a.title or rule.title or rule.id,
                severity=a.severity,
                target=a.target,
                message=a.message,
                color=a.color,
                blink=a.blink,
                zoom=a.zoom,
            )
            self._link(tails, alert_node)
        for phase, title in PHASES.items():
            actions: list[Action] | None = getattr(rule, phase)
            if not actions:
                continue
            group = self.node(f"phase:{phase}", "action_group", phase, title, phase=phase)
            sources = [alert_node] if alert_node and phase != "actions" else tails
            label = None if phase == "actions" else title
            for source in sources:
                self.edge(source, group, "flow", label)
            self._sequence(group, actions, phase, "")

    def _sequence(self, previous: str, actions: list[Action], phase: str, prefix: str) -> None:
        for i, action in enumerate(actions):
            path = f"{prefix}{i}"
            extra = action.model_extra or {}
            node = self.node(
                f"action:{phase}:{path}",
                "action",
                action.type,
                describe_action(action.type, extra),
                phase=phase,
                path=path,
                timeout=action.timeout,
                retries=action.retries,
            )
            self.edge(previous, node, "flow", f"{i + 1}" if not prefix else None)
            if action.type not in ("choose", "if"):
                found: set[str] = set()
                _action_entities(extra, found)
                for eid in sorted(found):
                    self.uses(node, eid)
            if action.type == "if":
                self._branch(node, phase, path, "then", extra["then"])
                if extra.get("else"):
                    self._branch(node, phase, path, "else", extra["else"])
            elif action.type == "choose":
                for n, choice in enumerate(extra["choices"]):
                    cond = describe_leaf(Condition.model_validate(choice["condition"]))
                    self._branch(node, phase, path, f"choice{n}", choice["then"], cond)
                if extra.get("default"):
                    self._branch(node, phase, path, "default", extra["default"], "sonst")
            previous = node

    def _branch(
        self,
        parent: str,
        phase: str,
        path: str,
        name: str,
        items: list[Any],
        label: str | None = None,
    ) -> None:
        actions = [Action.model_validate(i) for i in items]
        first = f"action:{phase}:{path}.{name}.0"
        self._sequence(parent, actions, phase, f"{path}.{name}.")
        for edge in self.edges:
            if edge["target"] == first and edge["source"] == parent:
                edge["kind"] = "branch"
                edge["label"] = label or name


def build_graph(
    rule: Rule,
    entity: EntityLookup = lambda _eid: None,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Graph einer Regel; mit `run` (Zeile aus `rule_runs`) wird der genommene Pfad markiert."""
    builder = _Builder(rule, entity)
    builder.build()
    graph: dict[str, Any] = {
        "rule_id": rule.id,
        "nodes": list(builder.nodes.values()),
        "edges": builder.edges,
        "layout_hints": LAYOUT_HINTS,
    }
    if run is not None:
        graph["run"] = _overlay(rule, graph, run)
    return graph


# --- Lauf-Trace ---------------------------------------------------------------------


def _overlay(rule: Rule, graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    taken: set[str] = set()
    info: dict[str, dict[str, Any]] = {}

    def mark(
        node_id: str, result: bool | None, detail: str = "", *, path: bool | None = None
    ) -> None:
        if node_id not in nodes:
            return
        info[node_id] = {"result": result, "detail": detail}
        if result is True if path is None else path:
            taken.add(node_id)

    trigger = run.get("trigger") or {}
    index = trigger.get("index")
    if rule.triggers and isinstance(index, int):
        mark(f"trigger:{index}", True, str(trigger.get("entity_id") or ""), path=True)
    elif not rule.triggers:
        mark("trigger:derived", True, str(trigger.get("type") or ""), path=True)
    else:  # Level-Übergang (Alert), kein bestimmter Trigger
        taken.update(n["id"] for n in graph["nodes"] if n["kind"] == "trigger")

    root = run.get("condition_trace") or {}
    parts = list(root.get("children") or [])
    if rule.active is not None and parts:
        win = parts.pop(0)
        mark("window", win.get("result"), win.get("detail", ""))
    if rule.condition is not None and parts:
        _walk(rule.condition, parts[0], "c", mark)
    if "for" in nodes:
        mark("for", True if run.get("phase") == "on_activate" else None, "", path=None)

    phase = run.get("phase", "actions")
    if "alert" in nodes and phase in ("on_activate", "on_clear"):
        mark("alert", True, phase, path=True)
    if f"phase:{phase}" in nodes:
        mark(f"phase:{phase}", True, "", path=True)
    for step in run.get("actions") or []:
        node_id = f"action:{phase}:{step.get('path')}"
        mark(
            node_id,
            step.get("status") == "ok",
            step.get("error") or step.get("detail") or "",
            path=True,
        )
        if node_id in info:
            info[node_id]["status"] = step.get("status")
            info[node_id]["duration_ms"] = step.get("duration_ms")
    highlighted = [
        e["id"]
        for e in graph["edges"]
        if e["kind"] != "uses" and e["source"] in taken and e["target"] in taken
    ]
    return {
        "run_id": run.get("run_id"),
        "phase": phase,
        "result": run.get("result"),
        "nodes": info,
        "highlight": {"nodes": sorted(taken), "edges": highlighted},
    }


def _walk(
    cond: Condition,
    trace: dict[str, Any],
    path: str,
    mark: Callable[..., None],
) -> None:
    result, detail = trace.get("result"), trace.get("detail", "")
    kids = trace.get("children") or []
    children = cond.and_ if cond.and_ is not None else cond.or_
    if children is None and cond.not_ is not None:
        children = [cond.not_]
    if children is None:
        mark(f"cond:{path}", result, detail)
        return
    mark(f"cond:{path}", result, detail)
    mark(f"cond:{path}:merge", result, detail)
    for i, (child, kid) in enumerate(zip(children, kids, strict=False)):
        _walk(child, kid, f"{path}.{i}", mark)
