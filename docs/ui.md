# Web-UI (read-only)

Svelte + Vite, `@xyflow/svelte` (Darstellung), `elkjs` (Layout), komplett offline (kein CDN). Das Frontend rendert nur den Graphen aus `GET /ui/api/rules/{id}/graph`; es enthält keine Regel-Logik (siehe `docs/ui-api.md`).

## Ansichten (Hash-Routing)
- `#/` Regelliste: Suche (semantisch), Filter Area/Tag/Stufe/Status/`created_by`
- `#/rules/{id}` Flow, Intent, YAML (Highlighting), Versions-Diff, Läufe; „Trace“ hebt den genommenen Pfad hervor
- `#/devices/{entity_id}` Regeln je Entity (Trigger, Bedingung, Ziel, Alert)
- `#/overview` Gruppen nach Area/Tag/Ziel, Heatmap (UTC-Tage), Shadow-/Fehler-Regeln
- Live-Updates per SSE (`/ui/api/events`)

## Betrieb
- Multi-Stage-Dockerfile baut `ui/` und legt es nach `/app/ui` (`UI_DIST_DIR`); FastAPI liefert es unter `/ui` (nur GET/HEAD, sonst 405). Ohne Build läuft nur die API.
- `UI_FRAME_ANCESTORS` (Standard `self`) setzt CSP `frame-ancestors`, z. B. `self http://homeassistant.local:8123` für ein HA-Panel/iframe.
- `UI_TOKEN_FILE` optional; das Token wird per `?token=` übergeben und in `sessionStorage` gehalten (keine Cookies).

## Entwicklung
`make ui` (ci, check, Vitest, Build) · `cd ui && npm run dev` (Proxy auf `127.0.0.1:8080`) · `npx playwright test` (lokal `PW_CHANNEL=chrome`).

Grenzen: Docker-Build, echtes HA und HA-iframe ungetestet; SSE pollt im Sekundentakt.
