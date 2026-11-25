"""Prometheus-Metriken für Adapter- und Bridge-Zustand."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)


class BridgeMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self._counters = {
            name: Counter(
                f"smart_bridge_{name}_total",
                help_text,
                registry=self.registry,
            )
            for name, help_text in {
                "ha_connection_attempts": "Anzahl gestarteter HA-WebSocket-Verbindungen.",
                "ha_reconnects": "Anzahl HA-WebSocket-Wiederverbindungsversuche.",
                "ha_events": "Anzahl empfangener HA-Events.",
                "mqtt_connection_attempts": "Anzahl gestarteter MQTT-Verbindungen.",
                "mqtt_reconnects": "Anzahl MQTT-Wiederverbindungsversuche.",
                "mqtt_messages": "Anzahl empfangener MQTT-Nachrichten.",
                "background_task_failures": "Anzahl unerwartet beendeter Bridge-Hintergrundtasks.",
            }.items()
        }
        self._gauges = {
            name: Gauge(
                f"smart_bridge_{name}",
                help_text,
                registry=self.registry,
            )
            for name, help_text in {
                "ha_connected": "HA-WebSocket-Verbindung aktiv (1/0).",
                "ha_synced": "HA-Vollsync abgeschlossen (1/0).",
                "mqtt_connected": "MQTT-Verbindung aktiv (1/0).",
                "ha_mcp_available": "HA-MCP-Executor verfügbar (1/0).",
                "entities": "Anzahl bekannter Entities.",
                "stale_entities": "Anzahl als stale markierter Entities.",
                "rules": "Anzahl aktiver Regeln im Engine-Index.",
                "active_alerts": "Anzahl aktiver Alerts.",
                "actions_paused": "Aktionen pausiert (1/0).",
            }.items()
        }

    def inc(self, name: str) -> None:
        self._counters[name].inc()

    def set(self, name: str, value: float) -> None:
        self._gauges[name].set(value)

    def scrape(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
