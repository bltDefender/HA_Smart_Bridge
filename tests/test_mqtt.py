import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Self

import pytest

from smart_bridge.adapters.mqtt import STATUS_TOPIC, MqttAdapter, MqttNotConnectedError
from smart_bridge.backoff import Backoff
from smart_bridge.events import BridgeEvent


@dataclass
class Msg:
    topic: str
    payload: bytes
    retain: bool = False


class FakeClient:
    def __init__(self, script: list[Msg], fail: bool, log: list[tuple[str, Any]]) -> None:
        self._script, self._fail, self.log = script, fail, log

    async def __aenter__(self) -> Self:
        if self._fail:
            raise ConnectionError("boom")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def subscribe(self, topic: str) -> None:
        self.log.append(("sub", topic))

    async def publish(self, topic: str, payload: Any = None, retain: bool = False) -> None:
        self.log.append(("pub", (topic, payload, retain)))

    @property
    def messages(self) -> AsyncIterator[Msg]:
        async def gen() -> AsyncIterator[Msg]:
            for m in self._script:
                yield m
            await asyncio.Event().wait()

        return gen()


async def test_reconnects_subscribes_and_forwards() -> None:
    log: list[tuple[str, Any]] = []
    attempts = 0

    def factory() -> FakeClient:
        nonlocal attempts
        attempts += 1
        return FakeClient([Msg("a/b", b"hi")], fail=attempts == 1, log=log)

    got: list[BridgeEvent] = []
    delays: list[float] = []

    async def sink(e: BridgeEvent) -> None:
        got.append(e)

    async def sleep(d: float) -> None:
        delays.append(d)

    adapter = MqttAdapter(factory, sink, ["a/#"], backoff=Backoff(1, 8), sleep=sleep)
    task = asyncio.create_task(adapter.run())
    async with asyncio.timeout(5):
        while not got:
            await asyncio.sleep(0.01)
    task.cancel()
    assert delays == [1.0]
    assert got[0].data == {"topic": "a/b", "payload": "hi", "retain": False}
    assert ("sub", "a/#") in log and ("pub", (STATUS_TOPIC, "online", True)) in log


async def test_publish_requires_connection() -> None:
    adapter = MqttAdapter(lambda: FakeClient([], False, []), _noop)
    with pytest.raises(MqttNotConnectedError):
        await adapter.publish("x", "y")


async def _noop(_: BridgeEvent) -> None:
    return None


async def test_on_connect_runs_after_subscribe() -> None:
    log: list[tuple[str, Any]] = []

    async def hook() -> None:
        log.append(("hook", None))
        raise asyncio.CancelledError  # Stream blockiert sonst endlos

    adapter = MqttAdapter(lambda: FakeClient([], False, log), _noop, ["a/#"], on_connect=hook)
    with contextlib.suppress(asyncio.CancelledError):
        await adapter.run()
    kinds = [k for k, _ in log]
    assert kinds.index("hook") > kinds.index("sub")
