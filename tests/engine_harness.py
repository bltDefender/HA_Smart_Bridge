from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from smart_bridge.engine import Outcome, RuleEngine
from smart_bridge.engine.timers import Callback
from smart_bridge.rules import parse_rules_text
from smart_bridge.state import StateStore

BERLIN = ZoneInfo("Europe/Berlin")


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class FakeTimers:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.items: dict[Hashable, tuple[datetime, Callback]] = {}

    def schedule(self, key: Hashable, delay: float, callback: Callback) -> None:
        self.items[key] = (self.clock.now + timedelta(seconds=delay), callback)

    def cancel(self, key: Hashable) -> bool:
        return self.items.pop(key, None) is not None

    def cancel_all(self) -> None:
        self.items.clear()

    async def advance(self, seconds: float) -> None:
        target = self.clock.now + timedelta(seconds=seconds)
        while True:
            due = [(d, k) for k, (d, _) in self.items.items() if d <= target]
            if not due:
                break
            when, key = min(due, key=lambda x: x[0])
            _, cb = self.items.pop(key)
            self.clock.now = when
            await cb()
        self.clock.now = target


@dataclass
class Harness:
    store: StateStore
    engine: RuleEngine
    clock: FakeClock
    timers: FakeTimers
    out: list[Outcome] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    async def set(self, entity_id: str, state: str, **attrs: Any) -> None:
        await self.store.update(entity_id, state, attrs, self.clock.now)

    def kinds(self) -> list[str]:
        return [
            f"{type(o).__name__}:{getattr(o, 'phase', '')}:{o.rule_id}".replace("::", ":")
            for o in self.out
        ]


async def make(
    yaml_text: str,
    states: dict[str, str] | None = None,
    start: datetime = datetime(2026, 1, 5, 12, 0, tzinfo=BERLIN),  # Montag
    start_engine: bool = True,
    **engine_kwargs: Any,
) -> Harness:
    rules, errors = parse_rules_text(yaml_text)
    assert not errors, errors
    clock = FakeClock(start)
    timers = FakeTimers(clock)
    store = StateStore(now=clock)
    for eid, st in (states or {}).items():
        await store.update(eid, st, {}, clock.now)
    out: list[Outcome] = []

    async def sink(o: Outcome) -> None:
        out.append(o)

    engine = RuleEngine(store, sink, now=clock, timers=timers, zone=BERLIN, **engine_kwargs)
    h = Harness(store, engine, clock, timers, out)
    h.errors = engine.load(rules)
    store.subscribe(engine.on_state_change)
    if start_engine:
        await engine.start()
    return h


def reader(states: dict[str, str]) -> Callable[[str], Any]:
    return states.get
