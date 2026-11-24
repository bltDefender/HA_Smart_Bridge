# Regeln

Format: `config/rules/*.yaml` mit Schlüssel `rules:`. JSON Schema: [rule.schema.json](rule.schema.json) (`make schema`). Prüfen: `bridge validate --all`.

- Eine Regel braucht `alert` und/oder Aktionen sowie `triggers` und/oder `condition`.
- `tier` ist berechnet und in YAML verboten.
- Condition: genau ein Schlüssel (`state`, `numeric_state`, `template`, `time`, `sun`, `workday`, `and`, `or`, `not`).
- `date`: `on: "2026-12-24"` (einmalig), `on: "12-24"` + `yearly: true` (29.02. → 28.02. in Nicht-Schaltjahren) oder `from`/`to` (täglich im Bereich, mit `yearly` über den Jahreswechsel), Uhrzeit per `at` (Standard `00:00`).
- Missed-Fire je Zeit-Trigger: `catch_up: none | once | within` plus `catch_up_within: "00:30:00"` bei `within`.
- Aktionen (`actions`, `on_activate`, `on_clear`), Parameter werden beim Laden geprüft:
  - `ha_tool`: `tool`, `arguments` · `ha_service`: `service` (`domain.service`), `target`, `data`
  - `notify`: `message`, optional `title`, `service` (Standard `notify.notify`)
  - `mqtt_publish`: `topic`, `payload`, `retain` · `webhook`: `url`, `method`, `body`, `headers`
  - `delay`: `duration` · `wait_for`: `condition`, `timeout`, `continue_on_timeout`
  - `if`: `condition`, `then`, `else` · `choose`: `choices: [{condition, then}]`, `default`
  - je Aktion optional `timeout` und `retries`
- Beispiele: `config/rules/examples.yaml`.
