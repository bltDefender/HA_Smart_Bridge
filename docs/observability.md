# Betrieb: Bereitschaft und Metriken

## Bereitschaft

`GET /healthz` prüft, ob der HTTP-Prozess antwortet. `GET /readyz` prüft zusätzlich den Engine-Start sowie konfigurierte Adapter. Ein HA-Ausfall markiert den Zustand als `stale`, bis der nächste vollständige Sync erfolgreich ist; ein Stale-Flag allein erzeugt keine Zustandsänderung und löst keine Regel aus. Ein Qdrant-Ausfall ist weiterhin `degraded`, nicht `down`.

HA-Authentifizierungsfehler beenden den HA-Adapter bewusst ohne Retry. Andere Verbindungsfehler werden mit exponentiellem Backoff erneut versucht. MQTT-Verbindungen, die vor Ablauf von 30 Sekunden abbrechen, setzen das Backoff nicht zurück. Nach Hot-Reload werden entfernte MQTT-Filter direkt abbestellt.

## Prometheus

`GET /metrics` liefert das Prometheus-Textformat. Adapter-Counter sind monoton; Zustands-Gauges werden bei jeder Abfrage aktualisiert.

| Metrik | Bedeutung |
|---|---|
| `smart_bridge_ha_connection_attempts_total` | Gestartete HA-Verbindungen |
| `smart_bridge_ha_reconnects_total` | HA-Reconnects nach Verbindungsfehler |
| `smart_bridge_ha_events_total` | Eingegangene HA-Events |
| `smart_bridge_mqtt_connection_attempts_total` | Gestartete MQTT-Verbindungen |
| `smart_bridge_mqtt_reconnects_total` | MQTT-Reconnects |
| `smart_bridge_mqtt_messages_total` | Eingegangene MQTT-Nachrichten |
| `smart_bridge_background_task_failures_total` | Unerwartet fehlgeschlagene Hintergrundtasks |
| `smart_bridge_ha_connected`, `smart_bridge_ha_synced` | HA-Verbindungs-/Sync-Status (0/1) |
| `smart_bridge_mqtt_connected`, `smart_bridge_ha_mcp_available` | MQTT-/HA-MCP-Status (0/1) |
| `smart_bridge_entities`, `smart_bridge_stale_entities` | Anzahl aller bzw. stale Entities |
| `smart_bridge_rules`, `smart_bridge_active_alerts` | Aktive Regeln bzw. Alerts |
| `smart_bridge_actions_paused` | Kill-Switch pausiert Aktionen (0/1) |

Die Metriken haben keine Entity-, Regel- oder URL-Labels und erzeugen daher keine dynamische Kardinalität. `/metrics` ist wie die übrigen Bridge-Endpunkte im LAN bereitzustellen und bei Bedarf auf Netzwerkebene zu beschränken.

## Belastungs- und Zeittests

Die Tests validieren einen Vollsync mit 2.000 Entities sowie Zustandserhalt und Stale-Verhalten unter dieser Last (großzügiges 10-Sekunden-Limit). Scheduler-Tests decken Zeitzonenwechsel, Mitternacht und verpasste Auslösungen ab; Adaptertests prüfen Wiederverbindung, Backoff und Timeout-Aufräumen.
