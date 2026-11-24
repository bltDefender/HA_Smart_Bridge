from __future__ import annotations

import asyncio
from typing import Any

import pytest

from smart_bridge.alerts import AlertStore
from smart_bridge.engine import AlertActivated
from smart_bridge.engine.outcomes import ActionsRequested
from smart_bridge.rules import expand_per_entity, parse_rules_text
from smart_bridge.state import EntityState
from smart_bridge.templating import (
    TemplateError,
    Templater,
    bind_entity,
    check_syntax,
    referenced_entities,
)

from .engine_harness import BERLIN, make
from .test_executor import SVC, Env, FakeBackend, req


def tpl(states: dict[str, str] | None = None) -> Templater:
    from datetime import datetime

    data = {
        k: EntityState(k, v, {"unit": "x"}, datetime(2026, 1, 1, tzinfo=BERLIN))
        for k, v in (states or {}).items()
    }
    return Templater(data.get, lambda: datetime(2026, 1, 1, 12, tzinfo=BERLIN), BERLIN)


# --- Templating ---------------------------------------------------------------


def test_render_helpers_and_typed_values() -> None:
    t = tpl({"sensor.a": "5", "light.x": "on"})
    assert t.render("{{ states('sensor.a') | float + 1 }}") == "6.0"
    assert t.render("{{ is_state('light.x', ['on']) }}") == "True"
    assert t.render("{{ state_attr('sensor.a', 'unit') }}") == "x"
    assert t.render("{{ states('nope') }}") == "unknown"
    assert t.render("{{ now().hour }}") == "12"
    assert t.render_value("{{ 5 }}") == 5
    assert t.render_value("n={{ 5 }}") == "n=5"
    assert t.render_deep({"a": ["{{ 1 + 1 }}", "plain"], "b": 3}) == {"a": [2, "plain"], "b": 3}


def test_sandbox_and_errors() -> None:
    t = tpl()
    with pytest.raises(TemplateError):
        t.render("{{ undefined_name }}")
    with pytest.raises(TemplateError):
        t.render("{{ ''.__class__.__mro__[1].__subclasses__() }}")
    with pytest.raises(ValueError, match="invalid template"):
        check_syntax("{{ oops")


def test_entity_extraction_and_binding() -> None:
    found, dyn = referenced_entities("{{ states('a.b') == 'on' and is_state('c.d', 'x') }}")
    assert found == {"a.b", "c.d"} and not dyn
    assert referenced_entities("{{ states(entity_id) }}", {"entity_id": "e.f"}) == ({"e.f"}, False)
    assert referenced_entities("{{ states(x) }}")[1]
    assert bind_entity({"k": ["{{ entity_id }}", "{{entity_id}}x"]}, "a.b") == {
        "k": ["a.b", "a.bx"]
    }


def test_invalid_template_rejected_on_load() -> None:
    _, errs = parse_rules_text(
        "rules:\n- id: r\n  alert: {target: t, severity: info}\n"
        "  condition: {template: {value_template: '{{ oops'}}\n"
    )
    assert errs


# --- per_entity -----------------------------------------------------------------

PER_ENTITY = """
rules:
  - id: fenster
    per_entity: [binary_sensor.fenster_kueche, binary_sensor.fenster_bad]
    alert: { target: "{{ entity_id }}", severity: warning, message: "{{ entity_id }} offen" }
    condition:
      and:
        - { state: { entity_id: "{{ entity_id }}", is: "on" } }
        - { numeric_state: { entity_id: sensor.regen, above: 0.1 } }
"""


def test_expansion_ids_and_bindings() -> None:
    (rule,), _ = parse_rules_text(PER_ENTITY)
    a, b = expand_per_entity(rule)
    assert a.id == "fenster__binary_sensor_fenster_kueche" and b.id.endswith("fenster_bad")
    assert a.per_entity is None and a.parent_id == "fenster"
    assert a.alert and a.alert.target == "binary_sensor.fenster_kueche"
    assert a.condition and a.condition.entity_ids() == {
        "binary_sensor.fenster_kueche",
        "sensor.regen",
    }


async def test_per_entity_alerts_are_independent() -> None:
    h = await make(
        PER_ENTITY,
        {
            "binary_sensor.fenster_kueche": "off",
            "binary_sensor.fenster_bad": "off",
            "sensor.regen": "0",
        },
    )
    assert not h.errors
    await h.set("sensor.regen", "1")
    await h.set("binary_sensor.fenster_bad", "on")
    acts_ = [o for o in h.out if isinstance(o, AlertActivated)]
    assert [o.rule_id for o in acts_] == ["fenster__binary_sensor_fenster_bad"]
    await h.set("binary_sensor.fenster_kueche", "on")
    assert len([o for o in h.out if isinstance(o, AlertActivated)]) == 2


async def test_alert_message_is_rendered() -> None:
    h = await make(
        PER_ENTITY,
        {
            "binary_sensor.fenster_kueche": "on",
            "binary_sensor.fenster_bad": "off",
            "sensor.regen": "1",
        },
    )
    store = AlertStore(lambda: h.engine.rules, render=h.engine.templater.render)
    for o in h.out:
        await store.handle(o)
    (alert,) = store.alerts()
    assert alert.message == "binary_sensor.fenster_kueche offen"


# --- Template-Condition -----------------------------------------------------------

TEMPLATE_RULE = """
rules:
  - id: t
    alert: { target: t, severity: info }
    condition: { template: { value_template: "{{ states('sensor.a') | float > 3 and is_state('light.x', 'on') }}" } }
"""


async def test_template_condition_is_level_based_and_indexed() -> None:
    h = await make(TEMPLATE_RULE, {"sensor.a": "1", "light.x": "on"})
    assert h.engine.index.rules_for_entity("light.x") == {"t"}
    await h.set("sensor.a", "5")
    assert h.engine.alert_state("t").active
    await h.set("light.x", "off")
    assert not h.engine.alert_state("t").active


async def test_template_error_holds_state() -> None:
    h = await make(TEMPLATE_RULE.replace("| float", "| nope"), {"sensor.a": "5", "light.x": "on"})
    assert not h.engine.alert_state("t").active  # Fehler = unbekannt, keine Abweichung


# --- Debounce / Rate-Limit ------------------------------------------------------

ACTION_RULE = """
rules:
  - id: r
    {extra}
    triggers: [{{ type: state, entity_id: a.b }}]
    actions: [{{ type: notify, message: x }}]
"""


async def test_debounce_fires_once_with_last_trigger() -> None:
    h = await make(ACTION_RULE.format(extra='debounce: "00:00:10"'), {"a.b": "0"})
    for v in ("1", "2", "3"):
        await h.set("a.b", v)
        await h.timers.advance(4)
    assert h.out == []
    await h.timers.advance(10)
    (out,) = h.out
    assert isinstance(out, ActionsRequested) and out.trigger.data["to"] == "3"


async def test_max_runs_per_hour_sliding_window() -> None:
    h = await make(ACTION_RULE.format(extra="max_runs_per_hour: 2"), {"a.b": "0"})
    for i in range(1, 5):
        await h.set("a.b", str(i))
    assert len(h.out) == 2 and h.engine.rate_limited == {"r": 2}
    await h.timers.advance(3601)
    await h.set("a.b", "9")
    assert len(h.out) == 3


# --- Executor: mode & Templates -------------------------------------------------


def mode_env(mode: str) -> tuple[Env, asyncio.Event]:
    rules, _ = parse_rules_text(
        f"rules:\n- id: rule\n  mode: {mode}\n  triggers: [{{type: time, at: '10:00'}}]\n"
        "  actions: [{type: ha_service, service: light.turn_on}]\n"
    )
    env = Env(default_retries=0)
    env.rules = {r.id: r for r in rules}
    gate = asyncio.Event()

    async def slow(domain: str, service: str, data: Any, target: Any) -> None:
        env.backend.calls.append((service, data, target))
        await gate.wait()

    env.backend.call_service = slow  # type: ignore[method-assign]
    return env, gate


async def _spin() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_mode_single_skips_second_run() -> None:
    env, gate = mode_env("single")
    first = asyncio.create_task(env.ex.run(req(SVC, run_id="a")))
    await _spin()
    second = await env.ex.run(req(SVC, run_id="b"))
    assert second.result == "skipped"
    gate.set()
    assert (await first).result == "ok" and len(env.backend.calls) == 1


async def test_mode_parallel_runs_both() -> None:
    env, gate = mode_env("parallel")
    tasks = [asyncio.create_task(env.ex.run(req(SVC, run_id=i))) for i in "ab"]
    await _spin()
    assert len(env.backend.calls) == 2
    gate.set()
    assert [(await t).result for t in tasks] == ["ok", "ok"]


async def test_mode_restart_cancels_previous() -> None:
    env, gate = mode_env("restart")
    first = asyncio.create_task(env.ex.run(req(SVC, run_id="a")))
    await _spin()
    second = asyncio.create_task(env.ex.run(req(SVC, run_id="b")))
    await _spin()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second).result == "ok"
    assert {r["run_id"]: r["result"] for r in env.store.recent("rule")} == {
        "a": "cancelled",
        "b": "ok",
    }


async def test_mode_queued_runs_sequentially() -> None:
    env, gate = mode_env("queued")
    tasks = [asyncio.create_task(env.ex.run(req(SVC, run_id=i))) for i in "ab"]
    await _spin()
    assert len(env.backend.calls) == 1  # zweiter wartet
    gate.set()
    assert [(await t).result for t in tasks] == ["ok", "ok"]
    assert len(env.backend.calls) == 2


async def test_action_params_are_rendered() -> None:
    backend = FakeBackend("ws")
    env = Env(backend, templater=tpl({"sensor.a": "21"}))
    rules, _ = parse_rules_text(
        "rules:\n- id: rule\n  per_entity: [light.k]\n  triggers: [{type: time, at: '10:00'}]\n"
        "  actions: [{type: ha_service, service: light.turn_on}]\n"
    )
    env.rules = {r.id: r for r in expand_per_entity(rules[0])}
    r = req(
        {
            "type": "ha_service",
            "service": "light.turn_on",
            "target": {"entity_id": "{{ entity_id }}"},
            "data": {
                "brightness": "{{ states('sensor.a') | int * 2 }}",
                "who": "{{ trigger.type }}",
            },
        }
    )
    r = ActionsRequested("rule__light_k", r.run_id, r.phase, r.actions, r.trigger, r.trace)
    rec = await env.ex.run(r)
    assert rec.result == "ok"
    assert backend.calls == [
        ("light.turn_on", {"brightness": 42, "who": "time"}, {"entity_id": "light.k"})
    ]


async def test_template_error_in_action_fails_run() -> None:
    env = Env(templater=tpl())
    rec = await env.ex.run(req({**SVC, "data": {"x": "{{ nope }}"}}))
    assert rec.result == "error" and "template" in (rec.steps[0].error or "")
    assert env.backend.calls == []
