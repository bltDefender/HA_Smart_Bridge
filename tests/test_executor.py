from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from smart_bridge.engine.conditions import TraceNode
from smart_bridge.engine.outcomes import ActionsRequested, TriggerInfo
from smart_bridge.executor import ActionError, ActionExecutor, Dispatcher, RunStore
from smart_bridge.executor.runner import check_tools
from smart_bridge.rules import parse_rules_text
from smart_bridge.rules.models import Action, Condition

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeBackend:
    def __init__(self, name: str, failures: int = 0) -> None:
        self.name, self.failures, self.calls = name, failures, []  # type: ignore[var-annotated]
        self.tools: set[str] = set()

    async def call_service(self, domain: str, service: str, data: Any, target: Any) -> Any:
        self.calls.append((f"{domain}.{service}", data, target))
        if self.failures:
            self.failures -= 1
            raise ActionError("boom")

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        if self.failures:
            self.failures -= 1
            raise ActionError("boom")


def acts(*items: dict[str, Any]) -> tuple[Action, ...]:
    return tuple(Action.model_validate(i) for i in items)


def req(*items: dict[str, Any], run_id: str = "r1", shadow: bool = False) -> ActionsRequested:
    return ActionsRequested(
        "rule",
        run_id,
        "actions",
        acts(*items),
        TriggerInfo("time", T0),
        TraceNode("state", True),
        shadow,
    )


class Env:
    def __init__(self, backend: FakeBackend | None = None, **kw: Any) -> None:
        self.backend = backend or FakeBackend("ws")
        self.sleeps: list[float] = []
        self.truth: dict[str, bool] = {}
        self.rules: dict[str, Any] = {}
        self.store = RunStore()

        async def sleep(s: float) -> None:
            self.sleeps.append(s)
            await asyncio.sleep(0)

        self.ex = ActionExecutor(
            Dispatcher("ws", None, self.backend),
            self.store,
            lambda: self.rules,
            lambda _r, c: self.truth.get(c.state.entity_id if c.state else "", False),
            sleep=sleep,
            **kw,
        )


def cond(eid: str) -> dict[str, Any]:
    return {"state": {"entity_id": eid, "is": "on"}}


SVC = {"type": "ha_service", "service": "light.turn_on", "target": {"entity_id": "light.a"}}


async def test_ok_run_is_persisted() -> None:
    env = Env()
    rec = await env.ex.run(req(SVC))
    assert rec.result == "ok" and rec.executor == "ws"
    assert env.backend.calls == [("light.turn_on", None, {"entity_id": "light.a"})]
    saved = env.store.recent("rule")[0]
    assert saved["run_id"] == "r1" and saved["result"] == "ok"


async def test_retry_with_backoff_then_success() -> None:
    env = Env(FakeBackend("ws", failures=2), backoff_base=1.0)
    rec = await env.ex.run(req(SVC))
    assert rec.result == "ok" and rec.steps[0].attempts == 3
    assert env.sleeps == [1.0, 2.0]


async def test_failure_after_retries_stops_run() -> None:
    env = Env(FakeBackend("ws", failures=99))
    rec = await env.ex.run(req(SVC, {**SVC, "service": "light.turn_off"}))
    assert rec.result == "error" and len(rec.steps) == 1 and rec.steps[0].status == "error"


async def test_timeout_is_error() -> None:
    env = Env(default_retries=0)

    async def slow(*_: Any) -> None:
        await asyncio.sleep(10)

    env.backend.call_service = slow  # type: ignore[assignment,method-assign]
    rec = await env.ex.run(req({**SVC, "timeout": "00:00:00"}))
    assert rec.result == "error" and "timeout" in (rec.steps[0].error or "")


async def test_rate_limit() -> None:
    env = Env(max_actions_per_minute=2, default_retries=0)
    rec = await env.ex.run(req(SVC, SVC, SVC))
    assert rec.result == "error" and "rate limit" in (rec.steps[-1].error or "")
    assert len(env.backend.calls) == 2


async def test_idempotent_duplicate_is_skipped() -> None:
    env = Env()
    await env.ex.run(req(SVC, run_id="same"))
    rec = await env.ex.run(req(SVC, run_id="same"))
    assert rec.steps[0].status == "skipped" and len(env.backend.calls) == 1


async def test_delay_uses_sleep() -> None:
    env = Env()
    await env.ex.run(req({"type": "delay", "duration": "00:00:05"}, SVC))
    assert env.sleeps == [5.0] and len(env.backend.calls) == 1


async def test_if_and_choose_branches() -> None:
    env = Env()
    env.truth = {"x": True}
    off = {**SVC, "service": "light.turn_off"}
    await env.ex.run(
        req(
            {"type": "if", "condition": cond("x"), "then": [SVC], "else": [off]},
            {
                "type": "choose",
                "choices": [{"condition": cond("y"), "then": [off]}],
                "default": [{**SVC, "service": "light.toggle"}],
            },
        )
    )
    assert [c[0] for c in env.backend.calls] == ["light.turn_on", "light.toggle"]


async def test_wait_for_woken_by_state_change() -> None:
    env = Env()
    env.ex._sleep = asyncio.sleep  # noqa: SLF001  # echtes Warten, nur das Event weckt
    task = asyncio.create_task(env.ex.run(req({"type": "wait_for", "condition": cond("x")}, SVC)))
    for _ in range(5):
        await asyncio.sleep(0)
    env.truth["x"] = True
    env.ex.notify_state_changed()
    rec = await asyncio.wait_for(task, 2)
    assert rec.result == "ok" and len(env.backend.calls) == 1


@pytest.mark.parametrize(("cont", "result", "calls"), [(False, "error", 0), (True, "ok", 1)])
async def test_wait_for_timeout(cont: bool, result: str, calls: int) -> None:
    env = Env()
    rec = await env.ex.run(
        req(
            {
                "type": "wait_for",
                "condition": cond("x"),
                "timeout": "00:00:01",
                "continue_on_timeout": cont,
            },
            SVC,
        )
    )
    assert rec.result == result and len(env.backend.calls) == calls


async def test_non_live_modes_have_no_side_effects() -> None:
    env = Env(dry_run=True)
    assert (await env.ex.run(req(SVC))).result == "dry_run"
    assert (await Env().ex.run(req(SVC, shadow=True))).result == "shadow"
    assert env.backend.calls == []


async def test_rule_dry_run() -> None:
    env = Env()
    rules, _ = parse_rules_text(
        "rules:\n- id: rule\n  dry_run: true\n  triggers: [{type: time, at: '10:00'}]\n"
        "  actions: [{type: ha_service, service: light.turn_on}]\n"
    )
    env.rules = {r.id: r for r in rules}
    assert (await env.ex.run(req(SVC))).result == "dry_run"
    assert env.backend.calls == []


async def test_pause_flag_persists() -> None:
    env = Env()
    env.ex.set_paused(True)
    assert (await env.ex.run(req(SVC))).result == "paused" and env.backend.calls == []
    again = ActionExecutor(env.ex._d, env.store, lambda: {}, lambda *_: True)  # noqa: SLF001
    assert again.paused
    env.ex.set_paused(False)
    assert (await env.ex.run(req(SVC, run_id="r2"))).result == "ok"


async def test_mqtt_and_webhook() -> None:
    sent: list[Any] = []

    async def mqtt(t: str, p: str, r: bool) -> None:
        sent.append((t, p, r))

    async def http(m: str, u: str, h: dict[str, str], b: Any) -> int:
        sent.append((m, u, h["Idempotency-Key"]))
        return 200

    env = Env(mqtt_publish=mqtt, http=http)
    await env.ex.run(
        req(
            {"type": "mqtt_publish", "topic": "a/b", "payload": {"x": 1}, "retain": True},
            {"type": "webhook", "url": "http://h/x"},
        )
    )
    assert sent == [("a/b", '{"x": 1}', True), ("POST", "http://h/x", "r1:1")]


async def test_ha_tool_without_mcp_fails() -> None:
    env = Env()
    rec = await env.ex.run(req({"type": "ha_tool", "tool": "HassTurnOn", "arguments": {}}))
    assert rec.result == "error"


def test_check_tools_reports_missing() -> None:
    rules, errs = parse_rules_text(
        "rules:\n- id: r1\n  triggers: [{type: time, at: '10:00'}]\n"
        "  actions:\n  - {type: ha_tool, tool: Nope, arguments: {}}\n"
        "  - type: if\n    condition: {state: {entity_id: a.b, is: 'on'}}\n"
        "    then: [{type: ha_tool, tool: HassTurnOn, arguments: {}}]\n"
    )
    assert not errs
    assert check_tools(rules, {"HassTurnOn"}) == {"r1": "MCP tools not available: ['Nope']"}
    assert check_tools(rules, {"HassTurnOn", "Nope"}) == {}
    assert Condition.model_validate(cond("a.b"))
