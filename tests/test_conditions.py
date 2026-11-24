from datetime import UTC, datetime

import pytest

from smart_bridge.engine.conditions import EvalContext, clock_window, evaluate
from smart_bridge.rules import parse_rules_text
from smart_bridge.state import EntityState, StateStore
from tests.engine_harness import BERLIN

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=BERLIN)  # Montag


def _store(states: dict[str, str]) -> StateStore:
    s = StateStore()
    s._states = {k: EntityState(k, v, {}, NOW.astimezone(UTC)) for k, v in states.items()}
    return s


def _eval(cond_yaml: str, states: dict[str, str], mode: str = "ignore", now: datetime = NOW):  # type: ignore[no-untyped-def]
    rules, errors = parse_rules_text(
        f"rules: [{{id: r, alert: {{target: t, severity: info}}, condition: {cond_yaml}}}]"
    )
    assert not errors, errors
    cond = rules[0].condition
    assert cond
    return evaluate(cond, EvalContext(_store(states), now, BERLIN, mode))  # type: ignore[arg-type]


def test_state_and_numeric() -> None:
    assert _eval("{state: {entity_id: a.b, is: 'on'}}", {"a.b": "on"}).result is True
    assert _eval("{state: {entity_id: a.b, is: ['on','open']}}", {"a.b": "open"}).result is True
    assert _eval("{state: {entity_id: a.b, is: 'on'}}", {"a.b": "off"}).result is False
    assert _eval("{numeric_state: {entity_id: s.x, above: 1, below: 5}}", {"s.x": "3"}).result
    assert _eval("{numeric_state: {entity_id: s.x, above: 1}}", {"s.x": "1"}).result is False


def test_unavailable_ignore_vs_deviation() -> None:
    cond = "{state: {entity_id: a.b, is: 'on'}}"
    assert _eval(cond, {"a.b": "unavailable"}).result is None
    assert _eval(cond, {}).result is None  # unbekannte Entity
    assert _eval(cond, {"a.b": "unavailable"}, "deviation").result is True
    assert _eval("{state: {entity_id: a.b, is: 'unavailable'}}", {"a.b": "unavailable"}).result
    assert _eval("{numeric_state: {entity_id: s.x, above: 1}}", {"s.x": "abc"}).result is None


def test_three_valued_logic() -> None:
    a_on = "{state: {entity_id: a.b, is: 'on'}}"
    c_on = "{state: {entity_id: c.d, is: 'on'}}"
    states = {"a.b": "off", "c.d": "unavailable"}
    assert _eval(f"{{and: [{a_on}, {c_on}]}}", states).result is False  # False dominiert
    assert _eval(f"{{or: [{a_on}, {c_on}]}}", states).result is None
    assert _eval(f"{{not: {c_on}}}", states).result is None
    assert _eval(f"{{not: {a_on}}}", states).result is True


@pytest.mark.parametrize(
    ("hour", "minute", "weekday_index", "expected"),
    [
        (23, 0, 0, True),  # Mo 23:00 im Fenster Mo-Start
        (3, 0, 1, True),  # Di 03:00 gehört zum Fenster, das Montag begann
        (3, 0, 0, False),  # Mo 03:00 gehört zum Sonntagsfenster
        (12, 0, 0, False),
        (22, 29, 0, False),
        (6, 0, 1, False),  # before ist exklusiv
    ],
)
def test_clock_window_across_midnight_uses_start_weekday(
    hour: int, minute: int, weekday_index: int, expected: bool
) -> None:
    now = datetime(2026, 1, 5 + weekday_index, hour, minute, tzinfo=BERLIN)
    assert clock_window(now, "22:30", "06:00", ["mon"]) is expected


def test_time_and_workday_conditions() -> None:
    assert _eval("{time: {after: '08:00', before: '18:00'}}", {}).result is True
    assert _eval("{time: {after: '13:00'}}", {}).result is False
    assert _eval("{workday: true}", {}).result is True
    sat = datetime(2026, 1, 10, 12, 0, tzinfo=BERLIN)
    assert _eval("{workday: true}", {}, now=sat).result is False
    assert _eval("{workday: false}", {}, now=sat).result is True


def test_naive_now_rejected() -> None:
    with pytest.raises(ValueError):
        _ = EvalContext(_store({}), datetime(2026, 1, 1), BERLIN).local_now
