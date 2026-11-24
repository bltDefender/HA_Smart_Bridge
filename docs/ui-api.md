# UI-API (read-only)

Alle Routen liegen unter `/ui/api` und akzeptieren nur `GET`/`HEAD`. Ein Test enumeriert alle `/ui`-Routen und wertet jede andere Methode als Fehler. Optional schützt `UI_TOKEN` (bzw. `UI_TOKEN_FILE`) per `Authorization: Bearer …`, für EventSource auch als `?token=`.

## Endpunkte
| Pfad | Inhalt |
|---|---|
| `/rules` | Liste; Filter `q` (semantisch über den Katalog, sonst Text), `area`, `tag`, `tier`, `status`, `created_by` |
| `/rules/{id}` | Metadaten, YAML (read-only), `intent` |
| `/rules/{id}/graph?run_id=` | `{rule_id, nodes, edges, layout_hints[, run]}` |
| `/rules/{id}/runs` | letzte Läufe aus `rule_runs` |
| `/rules/{id}/versions`, `/rules/{id}/diff?from=&to=` | Versionshistorie, Unified Diff |
| `/entities`, `/entities/{id}/rules` | Geräte-Ansicht (Rollen: trigger, condition, action, alert) |
| `/overview` | Gruppen nach Area/Tag/Ziel, Heatmap, Shadow-/Fehler-Regeln |
| `/events` | SSE: `hello`, `state`, `run`, `alerts` (nur Änderungen) |

## Graph-Modell
- Knoten `{id, kind, type, label, column, data}`; Arten und Spalten: trigger 0; window/condition/for 1; entity 2; alert/action_group/action 3.
- Kanten `{id, source, target, kind, label}` mit `flow`, `branch` (then/else/choose), `uses` (zu Entity-Knoten).
- IDs: `trigger:{i}`/`trigger:derived`, `window`, `cond:c.0.1` (+`:merge`), `for`, `alert`, `phase:{actions|on_activate|on_clear}`, `action:{phase}:{pfad}` (Pfad wie `StepResult.path` des Executors), `entity:{entity_id}`.
- Entity-Knoten tragen Name, Area, Domain, Zustand und `stale`; Template-IDs (`{{ … }}`) werden nicht als Entity gezeigt.
- Lauf-Trace (`run_id`): `run.nodes` (Ergebnis je Knoten), `run.highlight` (Knoten und Kanten des genommenen Pfads).

Das Frontend rendert nur; der Builder ist die einzige Quelle der Darstellung.
