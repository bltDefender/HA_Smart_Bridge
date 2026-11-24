"""MQTT-Adapter: Subscribe, Publish, LWT-Availability, Reconnect mit Backoff."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol, Self

import aiomqtt

from ..backoff import Backoff
from ..events import BridgeEvent, EventSink
from ..settings import Settings

log = logging.getLogger(__name__)

STATUS_TOPIC = "smart_bridge/status"


class MqttMessage(Protocol):
    @property
    def topic(self) -> Any: ...
    @property
    def payload(self) -> Any: ...
    @property
    def retain(self) -> bool: ...


class MqttClient(Protocol):
    @property
    def messages(self) -> AsyncIterator[MqttMessage]: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def subscribe(self, topic: str) -> Any: ...
    async def publish(self, topic: str, payload: Any = None, retain: bool = False) -> None: ...


ClientFactory = Callable[[], MqttClient]


class MqttNotConnectedError(Exception):
    pass


def aiomqtt_factory(settings: Settings) -> ClientFactory:
    if settings.mqtt_host is None:
        raise ValueError("MQTT_HOST not set")
    host = settings.mqtt_host

    def make() -> MqttClient:
        return aiomqtt.Client(  # type: ignore[return-value]
            host,
            settings.mqtt_port,
            username=settings.mqtt_user,
            password=settings.mqtt_password,
            will=aiomqtt.Will(STATUS_TOPIC, "offline", retain=True),
        )

    return make


def _decode(payload: Any) -> str:
    if isinstance(payload, bytes | bytearray):
        return bytes(payload).decode("utf-8", errors="replace")
    return "" if payload is None else str(payload)


class MqttAdapter:
    def __init__(
        self,
        factory: ClientFactory,
        sink: EventSink,
        topics: Sequence[str] = (),
        *,
        backoff: Backoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._factory, self._sink = factory, sink
        self._topics = list(topics)
        self._backoff = backoff or Backoff()
        self._sleep = sleep
        self._client: MqttClient | None = None
        self.connected = False

    async def run(self) -> None:
        while True:
            try:
                async with self._factory() as client:
                    await client.publish(STATUS_TOPIC, "online", retain=True)
                    for topic in self._topics:
                        await client.subscribe(topic)
                    self._client, self.connected = client, True
                    self._backoff.reset()
                    async for msg in client.messages:
                        await self._sink(
                            BridgeEvent(
                                "mqtt",
                                "mqtt_message",
                                {
                                    "topic": str(msg.topic),
                                    "payload": _decode(msg.payload),
                                    "retain": msg.retain,
                                },
                            )
                        )
                log.warning("MQTT stream ended")
            except Exception as exc:  # neu verbinden statt sterben
                log.warning("MQTT connection lost: %r", exc)
            finally:
                self._client, self.connected = None, False
            await self._sleep(self._backoff.next_delay())

    async def publish(self, topic: str, payload: str | bytes, retain: bool = False) -> None:
        if self._client is None:
            raise MqttNotConnectedError(topic)
        await self._client.publish(topic, payload, retain=retain)
