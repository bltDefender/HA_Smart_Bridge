# smart-bridge

**smart-bridge** ist eine lokale Automations-Bridge neben Home Assistant (HA). Sie empfängt
Zustände und Ereignisse aus HA und MQTT, wertet YAML-Regeln eigenständig aus und führt
Aktionen aus. Der letzte Zustand, Regeln und Zeitlogik bleiben in der Bridge; HA liefert
Geräte und Integrationen und ist der bevorzugte Weg für Aktionen.

Die Bridge enthält außerdem einen semantischen Entity-/Regelkatalog, einen MCP-Server für
Claude und eine **read-only** Web-UI mit Regel-Flows. Der Betrieb ist für Docker Compose im
lokalen Netz ausgelegt; es gibt keine Cloud-LLM-Aufrufe.

## Funktionen

- **Eingänge:** HA-WebSocket mit Vollsync, Ereignissen und Reconnect; MQTT-Topics.
- **Regeln:** YAML als Single Source of Truth, Bedingungen, Trigger und Zeitplanung,
  Hot-Reload und Validierung.
- **Aktionen:** HA-MCP bzw. WebSocket-Fallback, MQTT und weitere Regel-Aktionen.
  Stufe-2-Aktionen laufen zunächst im Shadow-Modus und benötigen eine Freigabe über CLI
  oder den signierten HA-Webhook.
- **Persistenz:** SQLite für Zustands-Snapshots, Läufe, Historie, Alerts und Audit.
- **Katalog:** optionaler lokaler Qdrant-Index für semantische Suche; die Regelauswertung
  ist nicht von ihm abhängig.
- **Oberflächen:** FastAPI, optionaler MCP-Server und read-only Web-UI für Regeln, Flows,
  Geräte, Läufe und Übersicht.
- **Betrieb:** `/healthz`, `/readyz` und Prometheus-Metriken unter `/metrics`.

## Voraussetzungen

- Docker mit Docker Compose v2 (`docker compose`)
- Home Assistant WebSocket-Zugriff und ein Long-Lived Access Token für HA
- Netzwerkzugriff zwischen HA, MQTT-Broker und Bridge

Beim ersten Standard-Docker-Build wird das lokale Embedding-Modell von Hugging Face in das
Image geladen. Dafür ist während des Builds Internetzugriff nötig. Danach kann die Bridge
ohne Cloud-LLM-Dienste betrieben werden. Qdrant läuft als eigener, nur intern erreichbarer
Compose-Service.

## Schnellstart mit Docker Compose

1. Repository klonen und Konfiguration anlegen:

   ```sh
   cp .env.example .env
   ```

2. Docker Secrets vorbereiten. Compose erwartet beide Dateien, auch wenn der MCP-Server
   deaktiviert ist:

   ```sh
   install -m 700 -d secrets
   touch secrets/ha_token.txt secrets/mcp_server_token.txt
   chmod 600 secrets/ha_token.txt secrets/mcp_server_token.txt
   ```

   Trage den HA Long-Lived Access Token in `secrets/ha_token.txt` ein. Für den optionalen
   MCP-Server erzeuge ein zufälliges Token und speichere es in
   `secrets/mcp_server_token.txt`, zum Beispiel mit `openssl rand -hex 32`. Die Dateien
   dürfen nicht ins Repository committed werden.

3. Passe `.env` an:

   - `HA_URL`: HA-WebSocket-URL, typischerweise
     `ws://<ha-host>:8123/api/websocket`
   - `HA_MCP_URL`: URL des HA-MCP-Servers für Aktionen; nur konfigurieren, wenn dieser
     Endpoint in der eigenen HA-Installation verfügbar ist
   - `MQTT_HOST` und bei Bedarf `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD_FILE`
   - `LATITUDE`, `LONGITUDE`, `TZ` für lokale Sonnen- und Zeitberechnungen
   - Optional `UI_FRAME_ANCESTORS`, falls die UI in einem HA-iframe angezeigt wird

   `HA_TOKEN_FILE` und `MCP_SERVER_TOKEN_FILE` zeigen standardmäßig auf die von Compose
   eingebundenen Secret-Dateien. Die Werte in `.env` müssen zu den Docker-internen Pfaden
   passen, nicht zu den Host-Dateipfaden.

4. Image bauen und Dienste starten:

   ```sh
   docker compose up -d --build
   docker compose logs -f bridge
   ```

5. Status und Oberfläche prüfen:

   ```sh
   curl http://localhost:8080/healthz
   curl http://localhost:8080/readyz
   ```

   Die UI ist unter <http://localhost:8080/ui/> erreichbar. Die API enthält
   `/api/state`, `/api/alerts` und `/api/runs`; Metriken liefert `/metrics`.
   `/readyz` ist erst `ok`, wenn die Engine gestartet und die konfigurierten Adapter
   bereit sind. Ein Qdrant-Ausfall wird als `degraded` ausgewiesen; die Regelauswertung
   läuft unabhängig davon weiter.

Stoppen:

```sh
docker compose down
```

`docker compose down -v` ist normalerweise nicht nötig; persistente Daten liegen unter
`data/`. Regeln und Policy liegen unter `config/`.

## Regeln erstellen und prüfen

Regeln liegen als YAML in `config/rules/*.yaml`. Eine Datei verwendet einen `rules:`-Block.
Ein minimales Beispiel:

```yaml
rules:
  - id: regen_alert
    title: Regen erkannt
    alert:
      target: building.haus
      severity: warning
    condition:
      numeric_state:
        entity_id: sensor.regen_rate
        above: 0.1
```

Vor dem Start oder nach Änderungen Regeln validieren:

```sh
docker compose exec bridge bridge validate --all
```

Die Bridge beobachtet Regeldateien und lädt gültige Änderungen automatisch. Bei
ungültigem YAML bleibt die letzte gültige Fassung aktiv; der Fehler wird gemeldet. Das
Regelschema und die vollständige Syntax stehen in [`docs/rules.md`](docs/rules.md),
Beispiele in [`config/rules/examples.yaml`](config/rules/examples.yaml) und das JSON Schema
in [`docs/rule.schema.json`](docs/rule.schema.json).

Risikostufen richten sich nach dem Inhalt der Aktionen. Reine Alerts können sofort aktiv
sein; kritische Aktionen, etwa das Entriegeln oder Verriegeln von Schlössern, starten im
Shadow-Modus und führen die Aktion nicht aus. Freigabe und Ablehnung erfolgen außerhalb
von MCP und UI:

```sh
docker compose exec bridge bridge pending
docker compose exec bridge bridge approve <rule_id>
docker compose exec bridge bridge reject <rule_id>
```

Details zu Stufen, Validierung und Freigabe: [`docs/policy.md`](docs/policy.md).

## Web-UI

Die UI unter `/ui/` ist ausschließlich lesend:

- **Regeln:** Suche und Filter nach Area, Tag, Stufe, Status und Urheber
- **Regel-Detail:** Flow-Diagramm, YAML, Versions-Diff und Lauf-Trace
- **Geräte:** Regeln, die eine Entity als Trigger, Bedingung, Alert-Ziel oder Aktionsziel
  verwenden
- **Übersicht:** Gruppierung und Auslösehäufigkeit
- **Live-Updates:** Zustände, Läufe und Alerts per SSE

Es gibt keine Formulare oder Schreibaktionen in der UI. Optional schützt `UI_TOKEN_FILE`
die UI-API mit einem Bearer-Token. Die CSP-Einstellung `UI_FRAME_ANCESTORS` steuert, welche
Ursprünge die Oberfläche in einem iframe einbetten dürfen. Siehe
[`docs/ui.md`](docs/ui.md) und [`docs/ui-api.md`](docs/ui-api.md).

## MCP für Claude (optional)

MCP ist standardmäßig deaktiviert. Zum Aktivieren:

```dotenv
MCP_SERVER_ENABLED=true
MCP_SERVER_TOKEN_FILE=/run/secrets/mcp_server_token
```

Danach Bridge neu starten. Der Streamable-HTTP-Endpunkt ist `/mcp` und verlangt
`Authorization: Bearer <Token>`. Der Server stellt Such-, Zustands-, Erklärungs-,
Simulations- und Authoring-Tools bereit. Schreibende Regeländerungen durchlaufen die
gleiche Validierungs- und Policy-Pipeline wie Dateiänderungen. Stufe-2-Freigaben stehen
nicht über MCP zur Verfügung.

MCP nur im vertrauenswürdigen LAN erreichbar machen und das Token vertraulich behandeln.
Weitere Informationen: [`docs/mcp.md`](docs/mcp.md).

## Betrieb und Sicherheit

- `/healthz` prüft den HTTP-Prozess; `/readyz` prüft Engine und konfigurierte Adapter.
- `/metrics` exponiert Prometheus-Metriken ohne Entity- oder Regel-Labels.
- HA-Verbindungsverlust markiert gespeicherte Zustände als `stale`. Stale-Wechsel lösen
  keine Regeln aus; nach erfolgreichem Vollsync werden die Zustände aktualisiert.
- `/ui/*` akzeptiert ausschließlich `GET` und `HEAD`. Die übrigen Bridge-API-Routen
  umfassen auch schreibende Funktionen wie Pause/Resume, Webhooks und Freigabe-Webhook.
- Ports und Endpunkte nur im lokalen Netz bzw. hinter einer passenden Firewall verfügbar
  machen. MCP- und UI-Token sind keine allgemeine Zugriffskontrolle für sämtliche
  Bridge-API-Routen.
- `config/policy.yaml`, Secrets und Freigabedaten nicht für Claude/MCP beschreibbar
  machen. Keine Secrets in `.env` committen.

## Entwicklung und Tests

Python 3.12 und `uv` werden verwendet; für die UI zusätzlich Node.js und npm:

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run bridge validate --all
make ui
```

Für den lokalen UI-Dev-Server:

```sh
cd ui
npm ci
npm run dev
```

Die Entwicklungshinweise und Architekturgrenzen stehen in [`CLAUDE.md`](CLAUDE.md) und
[`docs/architecture.md`](docs/architecture.md).
