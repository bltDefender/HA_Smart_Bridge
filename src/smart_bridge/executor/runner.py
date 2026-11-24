"""Ausführung der von der Engine angeforderten Aktionen."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from ..clock import Now, utc_now
from ..engine.conditions import TraceNode
from ..engine.outcomes import ActionsRequested
from ..rules.models import Action, Condition, Rule
from ..timeutil import parse_duration
from .dispatch import Dispatcher
from .errors import ActionError, UnsupportedActionError
from .runs import RunRecord, RunStore, StepResult

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
MqttPublish = Callable[[str, str, bool], Awaitable[None]]
HttpRequest = Callable[[str, str, dict[str, str], Any], Awaitable[int]]
EvalCondition = Callable[[str, Condition], bool | None]

DEFAULT_TIMEOUT = 30.0
DEFAULT_WAIT_CAP = 3600.0
PAUSE_FLAG = "paused"


class _Stop(Exception):
    """Lauf abbrechen (Fehler in einem Schritt)."""


def trace_to_dict(trace: TraceNode) -> dict[str, Any]:
    return {
        "kind": trace.kind,
        "result": trace.result,
        "detail": trace.detail,
        "children": [trace_to_dict(c) for c in trace.children],
    }


def collect_tools(actions: list[Action]) -> set[str]:
    """Alle `ha_tool`-Namen einer Aktionsliste, auch in `choose`/`if`."""
    found: set[str] = set()
    for a in actions:
        extra = a.model_extra or {}
        if a.type == "ha_tool":
            found.add(str(extra["tool"]))
        for key in ("then", "else", "default"):
            found |= collect_tools([Action.model_validate(i) for i in extra.get(key) or []])
        for choice in extra.get("choices") or []:
            found |= collect_tools([Action.model_validate(i) for i in choice.get("then") or []])
    return found


def check_tools(rules: list[Rule], tools: set[str]) -> dict[str, str]:
    """Regeln, die nicht verfügbare MCP-Tools nutzen → `rule_id: Fehler`."""
    errors: dict[str, str] = {}
    for rule in rules:
        used = collect_tools(
            [*(rule.actions or []), *(rule.on_activate or []), *(rule.on_clear or [])]
        )
        missing = sorted(used - tools)
        if missing:
            errors[rule.id] = f"MCP tools not available: {missing}"
    return errors


class ActionExecutor:
    def __init__(
        self,
        dispatcher: Dispatcher,
        store: RunStore,
        rules: Callable[[], dict[str, Rule]],
        eval_condition: EvalCondition,
        *,
        mqtt_publish: MqttPublish | None = None,
        http: HttpRequest | None = None,
        now: Now = utc_now,
        sleep: Sleep = asyncio.sleep,
        dry_run: bool = False,
        default_retries: int = 2,
        backoff_base: float = 1.0,
        max_actions_per_minute: int = 120,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._d, self._store, self._rules = dispatcher, store, rules
        self._eval, self._mqtt, self._http = eval_condition, mqtt_publish, http
        self._now, self._sleep, self.dry_run = now, sleep, dry_run
        self._retries, self._backoff = default_retries, backoff_base
        self._limit, self._monotonic = max_actions_per_minute, monotonic
        self._window: deque[float] = deque()
        self._done: set[str] = set()
        self._changed = asyncio.Event()
        self.paused = store.get_flag(PAUSE_FLAG)

    # --- Kill-Switch -----------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._store.set_flag(PAUSE_FLAG, paused)

    def notify_state_changed(self) -> None:
        """Weckt `wait_for`-Schleifen; von StateStore-Listenern aufgerufen."""
        self._changed.set()

    # --- Lauf ------------------------------------------------------------

    async def run(self, req: ActionsRequested) -> RunRecord:
        started = self._now()
        t0 = self._monotonic()
        rule = self._rules().get(req.rule_id)
        mode = self._mode(req, rule)
        record = RunRecord(
            req.run_id,
            req.rule_id,
            req.phase,
            {
                "type": req.trigger.type,
                "at": req.trigger.at.isoformat(),
                "index": req.trigger.index,
                "entity_id": req.trigger.entity_id,
                "data": req.trigger.data,
            },
            trace_to_dict(req.trace),
            "ok",
            started,
        )
        used: list[str] = []
        try:
            if mode == "paused":
                record.result = "paused"
            else:
                await self._sequence(req, list(req.actions), "", mode, record, used)
                record.result = mode if mode in ("shadow", "dry_run") else "ok"
        except _Stop:
            record.result = "error"
        except Exception as exc:  # Programmfehler darf den Lauf nicht verschwinden lassen
            log.exception("run %s crashed", req.run_id)
            record.steps.append(StepResult("?", "internal", "error", error=repr(exc)))
            record.result = "error"
        record.duration_ms = int((self._monotonic() - t0) * 1000)
        record.executor = "+".join(sorted(set(used))) or "-"
        self._store.save(record)
        log.info(
            "run %s rule=%s phase=%s result=%s executor=%s",
            record.run_id,
            record.rule_id,
            record.phase,
            record.result,
            record.executor,
        )
        return record

    def _mode(self, req: ActionsRequested, rule: Rule | None) -> str:
        if self.paused:
            return "paused"
        if req.shadow:
            return "shadow"
        if self.dry_run or (rule is not None and rule.dry_run):
            return "dry_run"
        return "live"

    async def _sequence(
        self,
        req: ActionsRequested,
        actions: list[Action],
        prefix: str,
        mode: str,
        record: RunRecord,
        used: list[str],
    ) -> None:
        for i, action in enumerate(actions):
            path = f"{prefix}{i}"
            await self._step(req, action, path, mode, record, used)

    async def _step(
        self,
        req: ActionsRequested,
        action: Action,
        path: str,
        mode: str,
        record: RunRecord,
        used: list[str],
    ) -> None:
        extra = action.model_extra or {}
        t0 = self._monotonic()
        step = StepResult(path, action.type, "ok")

        def finish(status: str | None = None) -> None:
            if status:
                step.status = status
            step.duration_ms = int((self._monotonic() - t0) * 1000)
            record.steps.append(step)

        if action.type in ("choose", "if"):
            branch = self._branch(req.rule_id, action, extra)
            step.detail = f"branch={branch[0]}"
            finish()
            await self._sequence(req, branch[1], f"{path}.{branch[0]}.", mode, record, used)
            return
        if mode != "live":
            step.detail = json.dumps(extra, default=str, sort_keys=True)
            finish(mode)
            return
        try:
            if action.type == "delay":
                await self._sleep(parse_duration(str(extra["duration"])).total_seconds())
                step.attempts = 1
            elif action.type == "wait_for":
                await self._wait_for(req.rule_id, action, extra, step)
            else:
                key = f"{req.run_id}:{path}"
                step.idempotency_key = key
                if key in self._done:
                    step.detail = "duplicate (idempotent)"
                    finish("skipped")
                    return
                await self._leaf(req, action, extra, step, key, used)
                self._done.add(key)
        except ActionError as exc:
            step.status, step.error = "error", str(exc)
            finish()
            raise _Stop from exc
        finish()

    def _branch(
        self, rule_id: str, action: Action, extra: dict[str, Any]
    ) -> tuple[str, list[Action]]:
        def parse(items: list[Any] | None) -> list[Action]:
            return [Action.model_validate(i) for i in items or []]

        if action.type == "if":
            ok = self._eval(rule_id, Condition.model_validate(extra["condition"])) is True
            return ("then", parse(extra["then"])) if ok else ("else", parse(extra.get("else")))
        for n, choice in enumerate(extra["choices"]):
            if self._eval(rule_id, Condition.model_validate(choice["condition"])) is True:
                return f"choice{n}", parse(choice["then"])
        return "default", parse(extra.get("default"))

    async def _wait_for(
        self, rule_id: str, action: Action, extra: dict[str, Any], step: StepResult
    ) -> None:
        cond = Condition.model_validate(extra["condition"])
        limit = (
            parse_duration(action.timeout).total_seconds() if action.timeout else DEFAULT_WAIT_CAP
        )
        deadline = self._monotonic() + limit
        step.attempts = 1
        while self._eval(rule_id, cond) is not True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._changed.clear()
            waiter = asyncio.ensure_future(self._changed.wait())
            timer = asyncio.ensure_future(self._sleep(remaining))
            _, pending = await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            if timer.done() and not waiter.done():
                if self._eval(rule_id, cond) is True:
                    return
                break
        else:
            return
        if extra.get("continue_on_timeout", False):
            step.detail = "timeout (continued)"
            return
        raise ActionError("wait_for timed out")

    # --- Einzelaktionen --------------------------------------------------

    def _allow(self) -> None:
        now = self._monotonic()
        while self._window and now - self._window[0] >= 60:
            self._window.popleft()
        if len(self._window) >= self._limit:
            raise ActionError(f"rate limit: more than {self._limit} actions per minute")
        self._window.append(now)

    async def _leaf(
        self,
        req: ActionsRequested,
        action: Action,
        extra: dict[str, Any],
        step: StepResult,
        key: str,
        used: list[str],
    ) -> None:
        timeout = (
            parse_duration(action.timeout).total_seconds() if action.timeout else DEFAULT_TIMEOUT
        )
        retries = self._retries if action.retries is None else action.retries
        last: ActionError | None = None
        for attempt in range(1, retries + 2):
            step.attempts = attempt
            self._allow()
            try:
                executor = await asyncio.wait_for(self._once(action, extra, key), timeout)
            except UnsupportedActionError as exc:
                raise ActionError(str(exc)) from exc  # Wiederholen ändert nichts
            except TimeoutError:
                last = ActionError(f"timeout after {timeout:.0f}s")
            except ActionError as exc:
                last = exc
            else:
                step.executor = executor
                used.append(executor)
                return
            if attempt <= retries:
                await self._sleep(min(self._backoff * 2 ** (attempt - 1), 30.0))
        assert last is not None
        raise last

    async def _once(self, action: Action, extra: dict[str, Any], key: str) -> str:
        if action.type == "ha_tool":
            name, _ = await self._d.call_tool(
                str(extra["tool"]), dict(extra.get("arguments") or {})
            )
            return name
        if action.type in ("ha_service", "notify"):
            service = str(extra.get("service") or "notify.notify")
            domain, _, svc = service.partition(".")
            data = dict(extra.get("data") or {})
            if action.type == "notify":
                for field_ in ("message", "title"):
                    if field_ in extra:
                        data[field_] = extra[field_]
            name, _ = await self._d.call_service(domain, svc, data or None, extra.get("target"))
            return name
        if action.type == "mqtt_publish":
            if self._mqtt is None:
                raise ActionError("MQTT not configured")
            payload = extra["payload"]
            text = payload if isinstance(payload, str) else json.dumps(payload)
            try:
                await self._mqtt(str(extra["topic"]), text, bool(extra.get("retain", False)))
            except Exception as exc:
                raise ActionError(f"mqtt publish failed: {exc!r}") from exc
            return "mqtt"
        if action.type == "webhook":
            if self._http is None:
                raise ActionError("HTTP client not configured")
            headers = {**(extra.get("headers") or {}), "Idempotency-Key": key}
            try:
                status = await self._http(
                    str(extra.get("method", "POST")).upper(),
                    str(extra["url"]),
                    headers,
                    extra.get("body"),
                )
            except Exception as exc:
                raise ActionError(f"webhook failed: {exc!r}") from exc
            if status >= 400:
                raise ActionError(f"webhook returned HTTP {status}")
            return "webhook"
        raise ActionError(f"unsupported action type {action.type}")
