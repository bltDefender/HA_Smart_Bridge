"""MQTT-Discovery für `sensor.haus3d_alerts` (Schnittstelle zum 3D-Modell, fest)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

from ..adapters.mqtt import STATUS_TOPIC, MqttNotConnectedError
from ..clock import Now, utc_now
from .store import AlertStore

log = logging.getLogger(__name__)

DISCOVERY_TOPIC = "homeassistant/sensor/smart_bridge/haus3d_alerts/config"
STATE_TOPIC = "smart_bridge/haus3d_alerts/state"
ATTRIBUTES_TOPIC = "smart_bridge/haus3d_alerts/attributes"

Publish = Callable[[str, str, bool], Awaitable[None]]


def discovery_config() -> dict[str, Any]:
    return {
        "name": "Haus3D Alerts",
        "unique_id": "smart_bridge_haus3d_alerts",
        "object_id": "haus3d_alerts",
        "state_topic": STATE_TOPIC,
        "json_attributes_topic": ATTRIBUTES_TOPIC,
        "availability_topic": STATUS_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "icon": "mdi:alert-circle-outline",
        "device": {"identifiers": ["smart_bridge"], "name": "Smart Bridge"},
    }


class AlertPublisher:
    def __init__(
        self, store: AlertStore, publish: Publish, zone: ZoneInfo, now: Now = utc_now
    ) -> None:
        self._store, self._publish, self._zone, self._now = store, publish, zone, now

    async def publish_discovery(self) -> None:
        await self._publish(DISCOVERY_TOPIC, json.dumps(discovery_config()), True)

    async def publish_state(self) -> None:
        alerts = self._store.alerts()
        attrs = {
            "alerts": [a.to_json(self._zone) for a in alerts],
            "updated": self._now().astimezone(self._zone).isoformat(),
        }
        try:
            await self._publish(STATE_TOPIC, str(len(alerts)), True)
            await self._publish(ATTRIBUTES_TOPIC, json.dumps(attrs, ensure_ascii=False), True)
        except MqttNotConnectedError:
            log.debug("MQTT offline; state is republished on reconnect")

    async def on_connect(self) -> None:
        """Nach (Wieder-)Verbindung: Discovery und aktuellen Stand neu senden."""
        await self.publish_discovery()
        await self.publish_state()
