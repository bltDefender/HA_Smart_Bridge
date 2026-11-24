# Copilot-Anweisungen

- Stack: Python 3.12, asyncio, pydantic v2, `uv`; FastAPI; SQLite; Qdrant; Svelte + Vite (UI, offline).
- Architekturgrenzen: Quellen und Senken sind Adapter hinter `Protocol`s. Die RuleEngine kennt kein HA und kein Netzwerk. UI ist read-only (`GET`/`HEAD`). Katalog ist nur Lesehilfe.
- Tests: pytest + pytest-asyncio, Zeit und Zustand injizieren, keine Netzwerkzugriffe. Scheduler-Änderungen brauchen DST- und Missed-Fire-Tests.
- Qualität: `ruff`, `mypy --strict` müssen sauber sein.
- Namen: Code und Bezeichner englisch, Doku deutsch. Tz-aware Datetimes überall.
- Regel-Schema: nach Modelländerungen `make schema` ausführen.
- Siehe `CLAUDE.md` für Kernregeln.
