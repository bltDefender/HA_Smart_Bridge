from __future__ import annotations

from typing import Any

import pytest

from smart_bridge.rules import parse_rules_text
from smart_bridge.rules.models import Rule
from smart_bridge.ui import build_graph
from smart_bridge.ui.text import describe_trigger, offset_text

BIG = """rules:
- id: big
  title: "Großes Beispiel"
  triggers:
    - {type: sun, event: sunset, offset: "-00:15:00"}
    - {type: state, entity_id: binary_sensor.door, to: "on", for: "00:01:00"}
  active: {after: "22:30", before: "06:00"}
  condition:
    and:
      - {state: {entity_id: binary_sensor.door, is: "on"}}
      - or:
          - {numeric_state: {entity_id: sensor.rain, above: 0.1}}
          - not: {state: {entity_id: lock.front, is: "locked"}}
  for: "00:00:30"
  alert: {target: door.ng, severity: critical, title: "Tür", color: "#ff3b30"}
  actions:
    - {type: ha_service, service: light.turn_on, target: {entity_id: light.hof}}
    - {type: delay, duration: "00:00:05"}
    - type: choose
      choices:
        - condition: {state: {entity_id: sensor.mode, is: night}}
          then: [{type: ha_service, service: light.turn_off, target: {entity_id: light.hof}}]
      default: [{type: notify, message: hi}]
  on_clear:
    - {type: if, condition: {state: {entity_id: sensor.mode, is: day}}, then: [{type: delay, duration: "00:00:01"}]}
"""


def parse(text: str) -> Rule:
    rules, errors = parse_rules_text(text)
    assert not errors, errors
    return rules[0]


def lookup(eid: str) -> dict[str, Any] | None:
    if eid == "light.hof":
        return {"name": "Hoflicht", "area": "Hof", "state": "off", "stale": False}
    return None


def by_id(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in graph["nodes"]}


def edges(graph: dict[str, Any], kind: str | None = None) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in graph["edges"] if kind in (None, e["kind"])}


def test_graph_structure_columns_and_texts() -> None:
    g = build_graph(parse(BIG), lookup)
    nodes = by_id(g)
    assert g["layout_hints"]["direction"] == "RIGHT"
    assert nodes["trigger:0"]["label"] == "Sonnenuntergang − 15 min"
    assert nodes["trigger:1"]["label"] == "binary_sensor.door → on für 1 min"
    assert nodes["window"]["label"] == "Aktiv: 22:30–06:00 Uhr"
    assert nodes["for"]["label"] == "Mindestens 30 s"
    alert = nodes["alert"]["data"]
    assert (alert["severity"], alert["target"], alert["color"]) == (
        "critical",
        "door.ng",
        "#ff3b30",
    )
    cols = {n["kind"]: n["column"] for n in g["nodes"]}
    assert cols["trigger"] < cols["condition"] < cols["entity"] < cols["alert"]
    # Ids sind eindeutig, Kanten zeigen nur auf existierende Knoten
    assert len(nodes) == len(g["nodes"])
    assert all(s in nodes and t in nodes for s, t in edges(g))


def test_flow_edges_connect_triggers_window_condition_for_and_results() -> None:
    g = build_graph(parse(BIG))
    flow = edges(g, "flow")
    assert {("trigger:0", "window"), ("trigger:1", "window")} <= flow
    assert ("window", "cond:c") in flow  # Wurzel (and) ist der Eintritt
    assert ("cond:c:merge", "for") in flow and ("for", "alert") in flow
    assert ("for", "phase:actions") in flow
    assert ("alert", "phase:on_clear") in flow  # Übergänge hängen am Alert
    branches = edges(g, "branch")
    assert ("cond:c", "cond:c.0") in branches and ("cond:c.0", "cond:c:merge") in branches
    assert ("cond:c.1", "cond:c.1.0") in branches and (
        "cond:c.1.1:merge",
        "cond:c.1:merge",
    ) in branches


def test_entities_are_deduplicated_with_live_badge_data() -> None:
    g = build_graph(parse(BIG), lookup)
    nodes = by_id(g)
    hof = nodes["entity:light.hof"]
    assert hof["label"] == "Hoflicht" and hof["data"]["area"] == "Hof"
    assert hof["data"]["state"] == "off" and hof["data"]["domain"] == "light"
    assert sum(1 for n in g["nodes"] if n["id"] == "entity:binary_sensor.door") == 1
    uses = edges(g, "uses")
    assert ("trigger:1", "entity:binary_sensor.door") in uses
    assert ("cond:c.0", "entity:binary_sensor.door") in uses
    assert ("action:actions:0", "entity:light.hof") in uses
    assert nodes["entity:lock.front"]["data"]["known"] is False


def test_actions_keep_order_delays_and_choose_branches() -> None:
    g = build_graph(parse(BIG))
    assert ("phase:actions", "action:actions:0") in edges(g, "flow")
    assert ("action:actions:0", "action:actions:1") in edges(g, "flow")
    assert ("action:actions:1", "action:actions:2") in edges(g, "flow")
    assert by_id(g)["action:actions:1"]["label"] == "Warte 5 s"
    labels = {(e["source"], e["target"]): e["label"] for e in g["edges"] if e["kind"] == "branch"}
    assert labels[("action:actions:2", "action:actions:2.choice0.0")] == "sensor.mode ist night"
    assert labels[("action:actions:2", "action:actions:2.default.0")] == "sonst"
    assert ("action:on_clear:0", "action:on_clear:0.then.0") in edges(g, "branch")
    assert {e["id"] for e in g["edges"]} == {f"{e['source']}->{e['target']}" for e in g["edges"]}


def test_derived_triggers_for_guard_rules_and_no_condition_rules() -> None:
    guard = build_graph(
        parse(
            "rules:\n- id: w\n  alert: {target: battery, severity: info}\n"
            "  condition: {numeric_state: {entity_id: sensor.bat, below: 10}}\n"
        )
    )
    nodes = by_id(guard)
    assert nodes["trigger:derived"]["data"]["derived"] is True
    assert "sensor.bat" in nodes["trigger:derived"]["label"]
    assert ("trigger:derived", "cond:c") in edges(guard, "flow") and ("cond:c", "alert") in edges(
        guard, "flow"
    )
    timed = build_graph(
        parse("rules:\n- id: t\n  triggers: [{type: time, at: '22:30'}]\n"
              "  actions: [{type: ha_service, service: light.turn_on, target: {entity_id: light.a}}]\n")
    )  # fmt: skip
    assert ("trigger:0", "phase:actions") in edges(timed, "flow")
    assert not any(n["kind"] == "condition" for n in timed["nodes"])


def test_per_entity_template_ids_are_not_rendered_as_entities() -> None:
    rule = parse(
        "rules:\n- id: p\n  per_entity: [binary_sensor.a]\n  alert: {target: '{{ entity_id }}', severity: warning}\n"
        "  condition: {state: {entity_id: '{{ entity_id }}', is: 'on'}}\n"
    )
    assert not [n for n in build_graph(rule)["nodes"] if n["kind"] == "entity"]


@pytest.mark.parametrize(
    ("snippet", "text"),
    [
        ("{type: time, at: '22:30', weekdays: [mon, fri]}", "Um 22:30 Uhr (Mo, Fr)"),
        ("{type: sun, event: civil_dawn, offset: '01:00:00'}", "Bürgerliche Morgendämmerung + 1 h"),
        ("{type: sun, event: sunrise}", "Sonnenaufgang"),
        ("{type: cron, expression: '*/5 * * * *'}", "Cron */5 * * * *"),
        ("{type: interval, every: '00:10:00'}", "Alle 10 min"),
        ("{type: date, on: '12-24', yearly: true, at: '18:00'}", "Am 12-24 (jährlich) um 18:00"),
        ("{type: mqtt, topic: a/b, payload: x}", "MQTT a/b = x"),
        ("{type: webhook, webhook_id: w1}", "Webhook w1"),
        ("{type: startup}", "Start der Bridge"),
        ("{type: numeric_state, entity_id: sensor.x, above: 5, hysteresis: 1}",
         "sensor.x über 5 (Hysterese 1)"),
    ],
)  # fmt: skip
def test_trigger_texts(snippet: str, text: str) -> None:
    rule = parse(
        f"rules:\n- id: t\n  triggers: [{snippet}]\n"
        "  actions: [{type: delay, duration: '00:00:01'}]\n"
    )
    assert describe_trigger(rule.triggers[0]) == text  # type: ignore[index]


def test_offset_text() -> None:
    assert offset_text("-00:15:00") == "− 15 min"
    assert offset_text("00:00:00") == ""
    assert offset_text("+01:30:00".lstrip("+")) == "+ 1 h 30 min"


# --- Lauf-Trace --------------------------------------------------------------------------------------------


def _trace(result: bool, door: bool, rain: bool | None, locked: bool) -> dict[str, Any]:
    def leaf(r: bool | None, d: str = "") -> dict[str, Any]:
        return {"kind": "state", "result": r, "detail": d, "children": []}

    not_node = {"kind": "not", "result": not locked, "detail": "", "children": [leaf(locked)]}
    or_node = {
        "kind": "or",
        "result": (rain or not locked) or None,
        "detail": "",
        "children": [leaf(rain), not_node],
    }
    cond = {"kind": "and", "result": result, "detail": "", "children": [leaf(door), or_node]}
    window = {"kind": "time_window", "result": True, "detail": "", "children": []}
    return {"kind": "rule", "result": result, "detail": "", "children": [window, cond]}


def test_run_overlay_marks_taken_path() -> None:
    run = {
        "run_id": "r1",
        "phase": "actions",
        "result": "ok",
        "trigger": {"type": "state", "index": 1, "entity_id": "binary_sensor.door"},
        "condition_trace": _trace(True, True, True, True),
        "actions": [
            {"path": "0", "status": "ok", "duration_ms": 3},
            {"path": "1", "status": "ok"},
            {"path": "2", "status": "ok", "detail": "branch=choice0"},
            {"path": "2.choice0.0", "status": "error", "error": "boom"},
        ],
    }
    g = build_graph(parse(BIG), lookup, run)
    info = g["run"]
    hl = set(info["highlight"]["nodes"])
    assert {
        "trigger:1",
        "window",
        "cond:c",
        "cond:c.0",
        "cond:c.1",
        "cond:c.1.0",
        "cond:c:merge",
    } <= hl
    assert "trigger:0" not in hl and "cond:c.1.1" not in hl  # Not(locked)=False
    assert info["nodes"]["cond:c.1.1"]["result"] is False
    assert info["nodes"]["action:actions:2.choice0.0"]["status"] == "error"
    assert "action:actions:2.default.0" not in hl and "phase:actions" in hl
    taken = set(info["highlight"]["edges"])
    assert "trigger:1->window" in taken and "action:actions:0->action:actions:1" in taken
    assert "trigger:0->window" not in taken
    assert not any(e.endswith("entity:light.hof") for e in taken)  # nur Flusskanten


def test_run_overlay_for_level_transition_and_unknown_condition() -> None:
    rule = parse(BIG)
    run = {
        "run_id": "r2",
        "phase": "on_activate",
        "result": "ok",
        "trigger": {"type": "level", "index": None},
        "condition_trace": _trace(False, True, None, True),
        "actions": [],
    }
    g = build_graph(rule, run=run)
    assert {"trigger:0", "trigger:1", "alert"} <= set(g["run"]["highlight"]["nodes"])
    assert g["run"]["nodes"]["cond:c.1.0"]["result"] is None
