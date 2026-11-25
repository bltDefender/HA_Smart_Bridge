# MCP-Server

Endpunkt: `POST/GET /mcp` (Streamable HTTP). Aktivierung: `MCP_SERVER_ENABLED=true` und `MCP_SERVER_TOKEN` (Docker Secret, `MCP_SERVER_TOKEN_FILE`). Authentifizierung: `Authorization: Bearer <token>`. Nur im LAN erreichbar machen.

Alle Aufrufe (Tools, Resources, Prompts) landen im Audit-Log (`audit_log`, SQLite, plus JSON-Log). Rückgaben und Audit-Details enthalten nie Tokens oder Secrets.

## Tools (lesend, M9)

| Tool | Parameter | Zweck |
|---|---|---|
| `search_entities` | `query`, `domain?`, `area?`, `limit=10` | semantische Suche im Katalog |
| `get_state` | `entity_id` | aktueller Zustand inkl. `stale` |
| `list_alerts` | | aktive Alerts (Format des 3D-Modells) |
| `list_rules` | | alle Regeln mit Status, Ziel, Alert-Zustand |
| `search_rules` | `query`, `limit=10` | semantische Regelsuche (Dubletten) |
| `list_model_targets` | | Ziele aus `haus3d_targets` |
| `explain_rule` | `rule_id` | Klartext, `intent`, aktuelle Bewertung je Knoten |
| `simulate_rule` | `rule_yaml`, `at?` | Dry-Run gegen aktuellen/historischen Zustand, ohne Seiteneffekte |
| `backtest_rule` | `rule_yaml`, `days=7` | `state_history` nachspielen (nur Zustandswechsel) |
| `list_rule_versions` | `rule_id` | Versionen (ab M10) |

Ist der Katalog nicht erreichbar, melden die Suchtools einen Fehler („degraded“); alle anderen Tools laufen weiter.

## Resources und Prompts

`bridge://docs/rule-schema`, `bridge://docs/rule-examples`, `bridge://policy` (read-only), `bridge://rules/{id}`, `bridge://entities/{entity_id}`, `bridge://alerts`. Prompt `author_rule`: Entities suchen → bestehende Regeln prüfen → entwerfen → simulieren/backtesten → anlegen → mit `explain_rule` bestätigen.

## Schreibende Tools (M10)

`create_rule`, `update_rule`, `set_rule_enabled`, `delete_rule`, `rollback_rule`, `acknowledge_alert`, `set_entity_description`. Neue Tools brauchen Schema, Test, Audit-Eintrag und einen Eintrag in dieser Datei.

- Alle laufen durch `audited`; bei Regeländerungen enthält das Audit einen Unified-Diff (vorher/nachher).
- Rate-Limit für Regel-Schreibaufrufe: `limits.writes_per_hour` (Policy, Standard 30).
- `create_rule`/`update_rule` erwarten genau eine Regel. Gesetzt werden `intent`, `created_by: claude`; Commit-Autor `claude-mcp`, Message = `intent`. `status`/`tier` im YAML werden abgelehnt.
- `locked: true` blockiert alle Schreib-Tools (nur MCP, nicht Dateizugriff).
- `delete_rule` ist Soft-Delete (`config/rules/.deleted/`); `rollback_rule` stellt auch gelöschte Regeln wieder her. `create_rule` auf eine gelöschte ID wird abgelehnt.
- Stufe-2-Regeln starten als `shadow`; es gibt bewusst kein Freigabe-Tool (siehe `docs/policy.md`).
- `acknowledge_alert(id, snooze_minutes)`: 1..10080 Minuten, Alert muss aktiv sein. `set_entity_description`: Text 1..600 Zeichen, braucht den Katalog.
