from datetime import datetime

from smart_bridge.engine import ActionsRequested, AlertActivated, AlertCleared
from smart_bridge.events import BridgeEvent
from tests.engine_harness import BERLIN, make

DOOR = """
rules:
  - id: door
    alert: { target: door.ng, severity: critical }
    condition:
      and:
        - { state: { entity_id: binary_sensor.door, is: "on" } }
        - { state: { entity_id: lock.ng, is: "locked" } }
    for: "00:01:00"
"""


async def test_level_alert_with_for_activates_after_hold_and_clears() -> None:
    h = await make(DOOR, {"binary_sensor.door": "off", "lock.ng": "locked"})
    await h.set("binary_sensor.door", "on")
    assert h.out == [] and h.engine.alert_state("door").pending
    await h.timers.advance(59)
    assert h.out == []
    await h.timers.advance(2)
    assert [type(o) for o in h.out] == [AlertActivated]
    assert h.engine.alert_state("door").since == datetime(2026, 1, 5, 12, 1, tzinfo=BERLIN)
    await h.set("lock.ng", "unlocked")
    assert [type(o) for o in h.out] == [AlertActivated, AlertCleared]


async def test_for_is_cancelled_when_condition_flips_back() -> None:
    h = await make(DOOR, {"binary_sensor.door": "off", "lock.ng": "locked"})
    await h.set("binary_sensor.door", "on")
    await h.timers.advance(30)
    await h.set("binary_sensor.door", "off")
    assert not h.engine.alert_state("door").pending
    await h.timers.advance(120)
    assert h.out == []
    # Neustart der Wartezeit bei erneuter Abweichung
    await h.set("binary_sensor.door", "on")
    await h.timers.advance(61)
    assert [type(o) for o in h.out] == [AlertActivated]


async def test_start_evaluates_current_levels() -> None:
    yaml = DOOR.replace('for: "00:01:00"', "")
    h = await make(yaml, {"binary_sensor.door": "on", "lock.ng": "locked"})
    assert [type(o) for o in h.out] == [AlertActivated]


async def test_unavailable_holds_alert_state() -> None:
    yaml = DOOR.replace('for: "00:01:00"', "")
    h = await make(yaml, {"binary_sensor.door": "on", "lock.ng": "locked"})
    await h.set("lock.ng", "unavailable")
    assert [type(o) for o in h.out] == [AlertActivated]  # weder geklärt noch neu ausgelöst
    assert h.engine.alert_state("door").active


async def test_unavailable_deviation_activates() -> None:
    yaml = DOOR.replace('for: "00:01:00"', "on_unavailable: deviation")
    h = await make(yaml, {"binary_sensor.door": "unavailable", "lock.ng": "unavailable"})
    assert [type(o) for o in h.out] == [AlertActivated]


async def test_active_window_gates_alert_on_reevaluate() -> None:
    yaml = """
rules:
  - id: night
    alert: { target: t, severity: info }
    active: { after: "22:30", before: "06:00" }
    condition: { state: { entity_id: a.b, is: "on" } }
"""
    h = await make(yaml, {"a.b": "on"}, start=datetime(2026, 1, 5, 12, 0, tzinfo=BERLIN))
    assert h.out == []  # Bedingung wahr, aber außerhalb des Fensters
    h.clock.now = datetime(2026, 1, 5, 23, 0, tzinfo=BERLIN)
    await h.engine.reevaluate("night")  # Scheduler (M4) ruft das bei window_start
    assert [type(o) for o in h.out] == [AlertActivated]
    h.clock.now = datetime(2026, 1, 6, 6, 0, tzinfo=BERLIN)
    await h.engine.reevaluate("night")
    assert [type(o) for o in h.out] == [AlertActivated, AlertCleared]


async def test_on_activate_and_on_clear_actions() -> None:
    yaml = """
rules:
  - id: r
    condition: { state: { entity_id: a.b, is: "on" } }
    on_activate: [{ type: notify, message: hi }]
    on_clear: [{ type: notify, message: bye }]
"""
    h = await make(yaml, {"a.b": "off"})
    await h.set("a.b", "on")
    await h.set("a.b", "off")
    assert h.kinds() == ["ActionsRequested:on_activate:r", "ActionsRequested:on_clear:r"]


async def test_derived_trigger_runs_actions_only_when_condition_true() -> None:
    yaml = """
rules:
  - id: r
    condition: { state: { entity_id: a.b, is: "on" } }
    actions: [{ type: notify, message: x }]
"""
    h = await make(yaml, {"a.b": "off"})
    await h.set("a.b", "on")
    await h.set("a.b", "off")
    assert [o.rule_id for o in h.out] == ["r"]
    assert isinstance(h.out[0], ActionsRequested) and h.out[0].trace.result is True
    assert h.out[0].run_id


async def test_shadow_flag_set_on_shadow_rules() -> None:
    yaml = """
rules:
  - id: r
    status: shadow
    triggers: [{ type: startup }]
    actions: [{ type: ha_service, service: lock.lock }]
"""
    h = await make(yaml)
    assert isinstance(h.out[0], ActionsRequested) and h.out[0].shadow


async def test_state_trigger_from_to_and_for() -> None:
    yaml = """
rules:
  - id: r
    triggers: [{ type: state, entity_id: a.b, from: "off", to: "on", for: "00:00:10" }]
    actions: [{ type: notify, message: x }]
"""
    h = await make(yaml, {"a.b": "off"})
    await h.set("a.b", "on")
    await h.timers.advance(5)
    await h.set("a.b", "off")  # Gegenänderung bricht ab
    await h.timers.advance(20)
    assert h.out == []
    await h.set("a.b", "on")
    await h.timers.advance(11)
    assert h.kinds() == ["ActionsRequested:actions:r"]
    await h.set("a.b", "unavailable")
    await h.set("a.b", "on")  # from != off
    await h.timers.advance(20)
    assert len(h.out) == 1


async def test_state_trigger_ignores_first_appearance_and_attribute_changes() -> None:
    yaml = """
rules:
  - id: r
    triggers: [{ type: state, entity_id: a.b }]
    actions: [{ type: notify, message: x }]
"""
    h = await make(yaml)
    await h.set("a.b", "on")  # erstes Auftauchen
    await h.set("a.b", "on", brightness=3)  # nur Attribut
    assert h.out == []
    await h.set("a.b", "off")
    assert len(h.out) == 1


async def test_numeric_crossing_with_hysteresis() -> None:
    yaml = """
rules:
  - id: r
    triggers: [{ type: numeric_state, entity_id: s.t, above: 25, hysteresis: 2 }]
    actions: [{ type: notify, message: x }]
"""
    h = await make(yaml, {"s.t": "20"})
    for value, fired in [("26", 1), ("27", 1), ("24", 1), ("26", 1), ("22", 1), ("26", 2)]:
        await h.set("s.t", value)
        assert len(h.out) == fired, (value, len(h.out))


async def test_numeric_initially_above_does_not_fire() -> None:
    yaml = """
rules:
  - id: r
    triggers: [{ type: numeric_state, entity_id: s.t, above: 25 }]
    actions: [{ type: notify, message: x }]
"""
    h = await make(yaml, {"s.t": "30"})
    await h.set("s.t", "31")
    assert h.out == []
    await h.set("s.t", "10")
    await h.set("s.t", "26")
    assert len(h.out) == 1


async def test_numeric_for_cancels_when_leaving_range() -> None:
    yaml = """
rules:
  - id: r
    triggers: [{ type: numeric_state, entity_id: s.t, below: 10, for: "00:00:30" }]
    actions: [{ type: notify, message: x }]
"""
    h = await make(yaml, {"s.t": "50"})
    await h.set("s.t", "5")
    await h.timers.advance(10)
    await h.set("s.t", "50")
    await h.timers.advance(60)
    assert h.out == []
    await h.set("s.t", "5")
    await h.timers.advance(31)
    assert len(h.out) == 1


async def test_event_mqtt_webhook_and_startup_triggers() -> None:
    yaml = """
rules:
  - id: ev
    triggers: [{ type: event, event_type: my_event, event_data: { a: { b: 1 } } }]
    actions: [{ type: notify, message: x }]
  - id: mq
    triggers: [{ type: mqtt, topic: "home/+/temp", payload: "1" }]
    actions: [{ type: notify, message: x }]
  - id: wh
    triggers: [{ type: webhook, webhook_id: abc }]
    actions: [{ type: notify, message: x }]
  - id: st
    triggers: [{ type: startup }]
    actions: [{ type: notify, message: x }]
"""
    h = await make(yaml)
    assert [o.rule_id for o in h.out] == ["st"]
    h.out.clear()
    e = h.engine.on_event
    await e(BridgeEvent("ha", "my_event", {"a": {"b": 1, "c": 2}}))
    await e(BridgeEvent("ha", "my_event", {"a": {"b": 2}}))
    await e(BridgeEvent("ha", "other", {}))
    await e(BridgeEvent("mqtt", "mqtt_message", {"topic": "home/k/temp", "payload": "1"}))
    await e(BridgeEvent("mqtt", "mqtt_message", {"topic": "home/k/temp", "payload": "2"}))
    await e(BridgeEvent("mqtt", "mqtt_message", {"topic": "home/k/x/temp", "payload": "1"}))
    await e(BridgeEvent("webhook", "webhook", {"webhook_id": "abc"}))
    await e(BridgeEvent("webhook", "webhook", {"webhook_id": "nope"}))
    assert [o.rule_id for o in h.out] == ["ev", "mq", "wh"]


async def test_trigger_condition_gate_blocks_actions() -> None:
    yaml = """
rules:
  - id: r
    triggers: [{ type: event, event_type: go }]
    condition: { state: { entity_id: a.b, is: "on" } }
    actions: [{ type: notify, message: x }]
"""
    h = await make(yaml, {"a.b": "off"})
    await h.engine.on_event(BridgeEvent("ha", "go", {}))
    assert h.out == []
    await h.set("a.b", "on")
    await h.engine.on_event(BridgeEvent("ha", "go", {}))
    assert len(h.out) == 1


async def test_index_only_touches_affected_rules() -> None:
    yaml = """
rules:
  - id: one
    condition: { state: { entity_id: a.one, is: "on" } }
    alert: { target: t, severity: info }
  - id: two
    condition: { state: { entity_id: a.two, is: "on" } }
    alert: { target: t, severity: info }
"""
    h = await make(yaml, {"a.one": "off", "a.two": "off"})
    assert h.engine.index.rules_for_entity("a.one") == {"one"}
    assert h.engine.index.rules_for_entity("a.unrelated") == set()
    await h.set("a.two", "on")
    assert [o.rule_id for o in h.out] == ["two"]


async def test_stale_roundtrip_does_not_trigger() -> None:
    yaml = """
rules:
  - id: r
    triggers: [{ type: state, entity_id: a.b }]
    actions: [{ type: notify, message: x }]
"""
    from smart_bridge.state import EntityState

    h = await make(yaml, {"a.b": "on"})
    h.store.mark_all_stale()
    await h.store.replace_all([EntityState("a.b", "on", {}, h.clock.now)])
    assert h.out == []


async def test_unsupported_rules_are_reported_and_not_loaded() -> None:
    yaml = """
rules:
  - id: tmpl
    alert: { target: t, severity: info }
    condition: { template: { value_template: "{{ true }}" } }
  - id: sun
    alert: { target: t, severity: info }
    condition: { sun: { after: sunset } }
  - id: pe
    per_entity: [a.b]
    alert: { target: t, severity: info }
    condition: { state: { entity_id: a.b, is: "on" } }
  - id: off
    enabled: false
    alert: { target: t, severity: info }
    condition: { workday: true }
  - id: ok
    alert: { target: t, severity: info }
    condition: { workday: true }
"""
    h = await make(yaml)
    assert set(h.errors) == {"tmpl", "sun", "pe"}
    assert [o.rule_id for o in h.out] == ["ok"]


async def test_reload_keeps_state_of_unchanged_rules() -> None:
    yaml = DOOR.replace('for: "00:01:00"', "")
    h = await make(yaml, {"binary_sensor.door": "on", "lock.ng": "locked"})
    assert h.engine.alert_state("door").active
    from smart_bridge.rules import parse_rules_text

    h.engine.load(parse_rules_text(yaml)[0])
    await h.engine.evaluate_all()
    assert len(h.out) == 1  # keine doppelte Aktivierung
