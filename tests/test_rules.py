from pathlib import Path

import pytest

from smart_bridge.rules import load_dir, parse_rules_text

ROOT = Path(__file__).parent.parent


def _errors(text: str) -> list[str]:
    return parse_rules_text(text)[1]


def test_example_rules_validate() -> None:
    results = load_dir(ROOT / "config" / "rules")
    assert results and all(r.ok for r in results), [r.errors for r in results]
    assert {r.id for res in results for r in res.rules} >= {"ng_open_locked", "nachts_verriegeln"}


def test_committed_schema_is_current() -> None:
    import json

    from smart_bridge.rules import rules_json_schema

    committed = json.loads((ROOT / "docs" / "rule.schema.json").read_text(encoding="utf-8"))
    assert committed == rules_json_schema()


@pytest.mark.parametrize(
    "body",
    [
        "rules: [{id: a, condition: {state: {entity_id: x.y, is: 'on'}}}]",  # no alert/actions
        "rules: [{id: A, alert: {target: t, severity: info}, condition: {workday: true}}]",
        "rules: [{id: a, alert: {target: t, severity: bad}, condition: {workday: true}}]",
        "rules: [{id: a, tier: 0, alert: {target: t, severity: info}, condition: {workday: true}}]",
        "rules: [{id: a, alert: {target: t, severity: info}, condition: {workday: true, "
        "state: {entity_id: x.y, is: 'on'}}}]",
        "rules: [{id: a, alert: {target: t, severity: info}, condition: {numeric_state: "
        "{entity_id: x.y}}}]",
        "rules: [{id: a, actions: [{type: nope}], triggers: [{type: startup}]}]",
        "rules: [{id: a, actions: [{type: delay}], triggers: [{type: time, at: '25:00'}]}]",
        "rules: [{id: a, actions: [{type: delay}], triggers: [{type: time, at: '22:30', "
        "catch_up: within}]}]",
        "rules: [{id: a, alert: {target: t, severity: info}, triggers: [{type: startup}]},"
        " {id: a, alert: {target: t, severity: info}, triggers: [{type: startup}]}]",
        "not: a mapping with rules",
        "rules: [unclosed",
    ],
)
def test_invalid_rules_are_rejected(body: str) -> None:
    rules, errors = parse_rules_text(body)
    assert errors and not rules


def test_missed_fire_within_valid() -> None:
    text = (
        "rules: [{id: a, actions: [{type: delay}], "
        "triggers: [{type: time, at: '22:30', catch_up: within, catch_up_within: '00:30:00'}]}]"
    )
    rules, errors = parse_rules_text(text)
    assert not errors and len(rules) == 1


def test_condition_entity_ids_nested() -> None:
    text = (
        "rules: [{id: a, alert: {target: t, severity: info}, condition: {not: {or: ["
        "{state: {entity_id: a.b, is: 'on'}}, {numeric_state: {entity_id: c.d, above: 1}}]}}}]"
    )
    rules, errors = parse_rules_text(text)
    assert not errors
    cond = rules[0].condition
    assert cond is not None and cond.entity_ids() == {"a.b", "c.d"}
