# Regeln

Format: `config/rules/*.yaml` mit Schlüssel `rules:`. JSON Schema: [rule.schema.json](rule.schema.json) (`make schema`). Prüfen: `bridge validate --all`.

- Eine Regel braucht `alert` und/oder Aktionen sowie `triggers` und/oder `condition`.
- `tier` ist berechnet und in YAML verboten.
- Condition: genau ein Schlüssel (`state`, `numeric_state`, `template`, `time`, `sun`, `workday`, `and`, `or`, `not`).
- Missed-Fire je Zeit-Trigger: `catch_up: none | once | within` plus `catch_up_within: "00:30:00"` bei `within`.
- Beispiele: `config/rules/examples.yaml`.
