# Policy und Freigabe

`config/policy.yaml` ist für Claude/MCP nicht beschreibbar (`.claude/settings.json` deny, im Dev-Container read-only gemountet). Fehlt die Datei, gelten eingebaute Standards.

## Stufen (nach Inhalt, nicht nach Pfad)
- **0**: nur Alerts (und `delay`/`wait_for`) → sofort aktiv.
- **1**: nur Aktionen auf der Allowlist (`light.*`, `switch.*`, `notify.*`, `input_*`; `mqtt_publish`/`webhook`/`ha_tool` nur wenn explizit erlaubt) → sofort aktiv, Rate-Limit.
- **2**: alles andere (fail-safe), insbesondere `lock.*`, `alarm_control_panel.*`, `climate.*`, `valve.*`, Garage, Wallbox, Wärmepumpe/SG-Ready → Status `shadow`.
- `choose`/`if` werden rekursiv geprüft; Templates (`per_entity`) werden mit den gebundenen Entities aufgelöst. Die höchste Stufe gewinnt.

## Shadow und Freigabe
Shadow-Regeln werten Bedingungen aus, Aktionen werden nur geloggt und als Alert „Schattenregel hätte … ausgelöst“ gezeigt. Aktivierung:
- `bridge pending` · `bridge approve <id>` · `bridge reject <id>` (`docker compose exec bridge ...`)
- oder HA-Benachrichtigung mit Buttons (`APPROVAL_NOTIFY_SERVICE`, `BRIDGE_PUBLIC_URL`) → signierter, einmal nutzbarer Link `/api/approvals/{token}` (GET zeigt nur eine Bestätigungsseite, POST löst ein). Tokens laufen nach `APPROVAL_TTL_HOURS` ab.

Die Freigabe gilt für den Inhaltshash der Regel (ohne `intent`, `title`, `tags`, `version`, `status`, `locked`, `enabled`). Ändert sich Logik oder Aktion, fällt die Regel zurück auf `shadow`. Beides schreibt Audit-Einträge; MCP und UI haben keinen Zugriff.

## Schutzmechanismen
- **Pipeline** (jede Änderung, atomar): Schema/Pydantic → Entity-Existenz (mit Vorschlägen) → Modell-Ziele (Warnung) → Dubletten/Konflikte → Pflicht-Backtest → Stufe. Konflikte blockieren nur auf Stufe 2.
- **Schleifenschutz**: Zyklen aus ≥ 2 Regeln werden abgelehnt (Selbst-Schleifen nicht). Circuit-Breaker: mehr als `max_activations_per_hour` → Regel `disabled` plus Alert `trip_<id>`; Update oder Aktivieren setzt zurück.
- **Versionierung**: `config/rules/` ist ein Git-Repo (Autor `claude-mcp` bzw. `file`, Message = `intent`).
- **Datei-Watcher**: Ungültige Datei → letzte gültige Version bleibt aktiv, Alert `file_<name>`, nie im Verlauf.
