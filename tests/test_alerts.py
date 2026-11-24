from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smart_bridge.adapters.mqtt import MqttNotConnectedError
from smart_bridge.alerts import AlertPublisher, AlertStore, TargetCache
from smart_bridge.alerts.publisher import (
    ATTRIBUTES_TOPIC,
    DISCOVERY_TOPIC,
    STATE_TOPIC,
)
from smart_bridge.engine import AlertActivated, AlertCleared
from smart_bridge.engine.conditions import TraceNode
from smart_bridge.events import BridgeEvent
from smart_bridge.rules import parse_rules_text

from .engine_harness import BERLIN, FakeClock

RULES = """
rules:
  - id: a_info
    title: Info
    alert: { target: battery, severity: info }
    condition: { state: { entity_id: sensor.x, is: "on" } }
  - id: b_crit
    title: Kritisch
    alert:
      { target: door.ng, severity: critical, blink: true, color: "#ff3b30", zoom: false,
        message: "offen" }
    condition: { state: { entity_id: sensor.x, is: "on" } }
  - id: c_crit_early
    alert: { target: door.haus, severity: critical }
    condition: { state: { entity_id: sensor.x, is: "on" } }
"""
T0 = datetime(2026, 11, 22, 21, 41, 7, tzinfo=UTC)
TRACE = TraceNode("rule", True)


def make(db: Path | str = ":memory:") -> tuple[AlertStore, FakeClock]:
    rules = {r.id: r for r in parse_rules_text(RULES)[0]}
    clock = FakeClock(T0)
    return AlertStore(lambda: rules, db, clock), clock


async def on(store: AlertStore, rid: str, since: datetime) -> None:
    await store.handle(AlertActivated(rid, since, TRACE))


async def test_sorted_by_severity_then_since() -> None:
    store, _ = make()
    await on(store, "a_info", T0)
    await on(store, "b_crit", T0 + timedelta(minutes=5))
    await on(store, "c_crit_early", T0 + timedelta(minutes=1))
    assert [a.id for a in store.alerts()] == ["c_crit_early", "b_crit", "a_info"]


async def test_clear_removes_alert() -> None:
    store, _ = make()
    await on(store, "a_info", T0)
    await store.handle(AlertCleared("a_info", T0, TRACE))
    assert store.alerts() == []


async def test_max_20_alerts() -> None:
    store, _ = make()
    for i in range(25):
        await store.set_system(str(i), "t", "m")
    assert len(store.alerts()) == 20


async def test_since_survives_restart_and_reconcile_drops_stale(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    store, _ = make(db)
    await on(store, "a_info", T0)
    await on(store, "b_crit", T0)
    store.close()
    store, _ = make(db)
    await on(store, "a_info", T0 + timedelta(hours=9))  # Engine meldet nach Neustart neues since
    assert store.alerts()[0].since == T0
    await store.reconcile({"a_info"})  # b_crit ist inzwischen nicht mehr aktiv
    await on(store, "b_crit", T0 + timedelta(hours=9))
    by_id = {a.id: a for a in store.alerts()}
    assert by_id["b_crit"].since == T0 + timedelta(hours=9)


async def test_snooze_hides_until_expiry() -> None:
    store, clock = make()
    await on(store, "a_info", T0)
    await store.snooze("a_info", T0 + timedelta(minutes=30))
    assert store.alerts() == []
    clock.now = T0 + timedelta(minutes=31)
    assert [a.id for a in store.alerts()] == ["a_info"]


async def test_system_alerts_replace_and_keep_since() -> None:
    store, clock = make()
    await store.replace_system({"bad": "unsupported"})
    first = store.alerts()[0]
    clock.now = T0 + timedelta(hours=1)
    await store.replace_system({"bad": "still"})
    assert store.alerts()[0].since == first.since
    await store.replace_system({})
    assert store.alerts() == []


async def test_publisher_json_matches_model_interface() -> None:
    store, _ = make()
    await on(store, "b_crit", datetime(2026, 11, 22, 21, 41, 7, tzinfo=UTC))
    sent: dict[str, tuple[str, bool]] = {}

    async def publish(topic: str, payload: str, retain: bool) -> None:
        sent[topic] = (payload, retain)

    pub = AlertPublisher(store, publish, BERLIN, now=lambda: T0)
    await pub.on_connect()
    assert sent[STATE_TOPIC] == ("1", True)
    attrs = json.loads(sent[ATTRIBUTES_TOPIC][0])
    assert attrs["updated"].endswith("+01:00")
    assert attrs["alerts"] == [
        {
            "id": "b_crit",
            "target": "door.ng",
            "severity": "critical",
            "title": "Kritisch",
            "message": "offen",
            "blink": True,
            "color": "#ff3b30",
            "zoom": False,
            "since": "2026-11-22T22:41:07+01:00",
        }
    ]
    config, retain = sent[DISCOVERY_TOPIC]
    cfg = json.loads(config)
    assert retain and cfg["json_attributes_topic"] == ATTRIBUTES_TOPIC
    assert cfg["availability_topic"] == "smart_bridge/status"
    assert cfg["state_topic"] == STATE_TOPIC


async def test_optional_fields_omitted() -> None:
    store, _ = make()
    await on(store, "a_info", T0)
    assert set(store.alerts()[0].to_json()) == {
        "id",
        "target",
        "severity",
        "title",
        "message",
        "since",
    }


async def test_publish_state_tolerates_offline_mqtt() -> None:
    store, _ = make()

    async def publish(topic: str, payload: str, retain: bool) -> None:
        raise MqttNotConnectedError(topic)

    await AlertPublisher(store, publish, BERLIN).publish_state()


async def test_store_listener_triggers_publish() -> None:
    store, _ = make()
    calls: list[str] = []

    async def publish(topic: str, payload: str, retain: bool) -> None:
        calls.append(topic)

    store.subscribe(AlertPublisher(store, publish, BERLIN).publish_state)
    await on(store, "a_info", T0)
    assert STATE_TOPIC in calls


# --- Ziele ---------------------------------------------------------------


def _targets_event(*ids: str) -> BridgeEvent:
    return BridgeEvent(
        "ha",
        "haus3d_targets",
        {"targets": [{"target": t, "label": t, "entities": []} for t in ids], "version": 3},
    )


async def test_list_targets_command_is_fired() -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    async def fire(event: str, data: dict[str, object]) -> None:
        sent.append((event, data))

    await TargetCache(fire).request()
    assert sent == [("haus3d_command", {"action": "list_targets"})]


async def test_list_targets_failure_is_only_a_warning() -> None:
    async def fire(event: str, data: dict[str, object]) -> None:
        raise RuntimeError("offline")

    await TargetCache(fire).request()


def test_validation_warns_only_for_unknown_targets() -> None:
    rules, _ = parse_rules_text(
        RULES
        + """
  - id: d_unknown
    alert: { target: zone.keller, severity: info }
    condition: { state: { entity_id: sensor.x, is: "on" } }
  - id: e_entity
    alert: { target: light.flur, severity: info }
    condition: { state: { entity_id: sensor.x, is: "on" } }
  - id: f_templated
    alert: { target: "{{ entity_id }}", severity: info }
    condition: { state: { entity_id: sensor.x, is: "on" } }
"""
    )
    cache = TargetCache()
    assert cache.validate(rules, lambda e: False) == {}  # ohne Antwort keine Prüfung
    assert cache.on_event(_targets_event("zone.og", "door.ng", "door.haus", "battery"))
    warnings = cache.validate(rules, lambda e: e == "light.flur")
    assert set(warnings) == {"d_unknown"}
    assert cache.version == 3
    assert not cache.on_event(BridgeEvent("ha", "other"))
