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
- Templates (Jinja2, sandboxed, `templating.py`): in `condition.template.value_template`, `alert.title`/`message` und allen String-Parametern von Aktionen. Funktionen `states(id)`, `is_state(id, wert|liste)`, `state_attr(id, attr)`, `has_value(id)`, `now()`. In Aktionen zusätzlich `trigger.*` (`type`, `entity_id`, `from`, `to`, …), `rule_id`, `run_id`. Ein einzelner `{{ ausdruck }}` liefert Zahl/Bool statt String. Syntaxfehler lehnen die Regel beim Laden ab.
- Template-Conditions: Wahr = `true/on/yes/1`, falsch = `false/off/no/0/""`; Fehler oder anderes Ergebnis = unbekannt (Alert-Zustand wird gehalten). Die gelesenen Entities müssen konstant sein (`states('a.b')`), damit der Trigger-Index sie beobachten kann; berechnete Entity-IDs lehnen die Regel ab.
- `per_entity: [a.b, c.d]`: je Entity eine Regel mit der ID `<id>__<entity_id mit _>`; `{{ entity_id }}` wird in allen Strings ersetzt und ist in Templates als Variable verfügbar. Alert und `since` sind je Entity getrennt.
- `mode`: `single` (Standard, weitere Läufe werden als `skipped` gespeichert), `restart` (laufender Lauf wird abgebrochen, Ergebnis `cancelled`), `queued` (nacheinander, höchstens 10 wartend), `parallel`.
- `debounce: "00:00:10"`: Trailing-Entprellung, es feuert nur der letzte Trigger nach Ruhe (gilt nicht für `startup`).
- `max_runs_per_hour: n`: gleitendes Stundenfenster für `actions` und `on_activate`; weitere Läufe werden verworfen und geloggt. `on_clear` wird nie begrenzt.
- Beispiele: `config/rules/examples.yaml`.
