"""Beispielregeln für `bridge://docs/rule-examples` (werden im Test gegen das Schema geprüft)."""

RULE_EXAMPLES = """rules:
  - id: ng_open_locked
    intent: "Warne mich, wenn nach 22:30 die NG-Tür offen steht, obwohl sie verriegelt ist"
    title: "NG offen + verriegelt"
    alert: { target: door.ng, severity: critical }
    active: { after: "22:30", before: "06:00" }
    condition:
      and:
        - { state: { entity_id: binary_sensor.nuki_ng_door, is: "on" } }
        - { state: { entity_id: lock.nuki_ng, is: "locked" } }
    for: "00:01:00"

  - id: batterie_leer
    title: "Hausbatterie < 10 %"
    alert: { target: battery, severity: info }
    condition: { numeric_state: { entity_id: sensor.mtec_battery_soc, below: 10 } }

  - id: aussenlicht_sonnenuntergang
    intent: "Schalte das Außenlicht 15 Minuten vor Sonnenuntergang ein"
    triggers:
      - { type: sun, event: sunset, offset: "-00:15:00" }
    actions:
      - { type: ha_service, service: light.turn_on, target: { entity_id: light.aussen_hof } }

  - id: fenster_offen_regen
    title: "Fenster offen bei Regen"
    per_entity: [binary_sensor.fenster_kueche, binary_sensor.fenster_bad]
    alert: { target: "{{ entity_id }}", severity: warning }
    condition:
      and:
        - { state: { entity_id: "{{ entity_id }}", is: "on" } }
        - { numeric_state: { entity_id: sensor.regen_rate, above: 0.1 } }
"""

AUTHOR_RULE_PROMPT = """Lege aus dem Wunsch des Nutzers eine Regel an. Halte diesen Ablauf ein:

1. Entities suchen: `search_entities` (nie Entity-IDs raten).
2. Bestehende Regeln prüfen: `search_rules` und `list_rules`, Dubletten und Konflikte vermeiden.
3. Regel entwerfen: YAML nach `bridge://docs/rule-schema`, Beispiele in `bridge://docs/rule-examples`.
   Den Wunsch des Nutzers im Wortlaut als `intent` übernehmen. `tier` nicht angeben.
4. `simulate_rule` gegen den aktuellen Zustand, dann `backtest_rule` (mindestens 7 Tage).
   Feuert die Regel zu oft, Bedingung oder `for`/`debounce` anpassen.
5. Anlegen mit `create_rule` (ab M10 verfügbar). Kritische Aktionen (Stufe 2) laufen zuerst als
   `shadow` und brauchen eine Freigabe außerhalb von MCP; sie lassen sich nicht umgehen.
6. Dem Nutzer das Ergebnis mit `explain_rule` in Klartext bestätigen.

Wunsch des Nutzers: {request}
"""
