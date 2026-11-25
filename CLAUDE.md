# CLAUDE.md

smart-bridge: eigenständige Bridge neben Home Assistant (Receive, Carry, Process). Architektur: `docs/architecture.md`.

## Befehle
- `uv sync` · `uv run pytest` · `uv run ruff check . && uv run ruff format --check .` · `uv run mypy`
- `uv run bridge validate --all` · `make schema` (regeneriert `docs/rule.schema.json`)
- `docker compose up` (Profil `dev` für Mosquitto)

## Kernregeln
- YAML (`config/rules/*.yaml`) ist Source of Truth. Der Vektorindex ist abgeleitet und jederzeit neu aufbaubar.
- Regeländerungen laufen ausschließlich durch die Validierungs-Pipeline. Niemals Policy oder Freigaben umgehen.
- Stufe-2-Aktionen werden nie ohne Freigabe außerhalb von MCP und UI aktiv.
- Der Graph-Builder ist die einzige Quelle für die Darstellung, keine Logik im Frontend.
- `/ui` ist read-only, neue Endpunkte müssen den Route-Test bestehen.
- Zeitlogik nur mit tz-aware Datetimes. Jede Scheduler-Änderung braucht DST- und Missed-Fire-Tests.
- Jede neue Regel-Funktion braucht Backtest- und Rollback-Tests.
- Neue MCP-Tools brauchen: Schema, Test, Audit-Eintrag und Eintrag in `docs/mcp.md`.
- Stufe wird nach Inhalt bestimmt (`policy.py`); unbekannte Aktionen sind Stufe 2. Freigabe nur per CLI/Webhook (`approvals.py`), nie per MCP/UI.
- Die RuleEngine kennt kein HA und kein Netzwerk; Zeit und Zustand werden injiziert.
- Keine Cloud-Dienste, keine Secrets im Repo (`.env`, `secrets/`).

## Definition of Done
Tests grün, `ruff` und `mypy --strict` ohne Fehler, `docs/` und `.env.example` aktualisiert, `docker compose up` startet sauber, `/readyz` grün.
