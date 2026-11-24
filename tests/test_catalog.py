from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from smart_bridge.alerts import TargetCache
from smart_bridge.api import app_factory
from smart_bridge.catalog import (
    Catalog,
    CatalogConfig,
    CatalogUnavailableError,
    Descriptions,
    HashEmbedder,
    Registry,
    VectorIndex,
    entity_doc,
    rule_doc,
    rule_entities,
)
from smart_bridge.history import StateHistory
from smart_bridge.rules import parse_rules_text
from smart_bridge.state import EntityState, StateChange, StateStore

from .mock_server import running_mock

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
REGISTRY = Registry.from_lists(
    [
        {"entity_id": "light.aussen_hof", "device_id": "d1", "area_id": None, "name": "Außenlicht"},
        {"entity_id": "lock.nuki_ng", "device_id": "d2", "area_id": "ng"},
        {"entity_id": "sensor.temp_bad", "device_id": None, "area_id": "bad"},
        {"entity_id": "sensor.unbenutzt", "device_id": None, "area_id": None},
        {"entity_id": "automation.x", "device_id": None, "area_id": None},
    ],
    [
        {
            "id": "d1",
            "name": "Hof-Lampe",
            "manufacturer": "Hue",
            "model": "Outdoor",
            "area_id": "hof",
        },
        {"id": "d2", "name": "Nuki", "manufacturer": "Nuki", "model": "Pro", "area_id": "ng"},
    ],
    [
        {"area_id": "hof", "name": "Hof"},
        {"area_id": "ng", "name": "Nebengebäude"},
        {"area_id": "bad", "name": "Bad"},
    ],
)
RULES = """rules:
- id: ng_offen
  intent: "Warne mich, wenn die NG-Tür offen steht"
  alert: {target: door.ng, severity: critical}
  condition: {state: {entity_id: lock.nuki_ng, is: unlocked}}
- id: licht
  triggers: [{type: sun, event: sunset, offset: "-00:15:00"}]
  actions: [{type: ha_service, service: light.turn_on, target: {entity_id: light.aussen_hof}}]
"""


def _state(eid: str, state: str = "on", **attrs: Any) -> EntityState:
    return EntityState(eid, state, attrs, NOW)


async def _catalog(
    tmp_path: Path, config: CatalogConfig | None = None, client: AsyncQdrantClient | None = None
) -> tuple[Catalog, StateStore, VectorIndex]:
    store = StateStore(now=lambda: NOW)
    for eid in REGISTRY.entities:
        await store.update(eid, "on", {}, NOW)
    rules = {r.id: r for r in parse_rules_text(RULES)[0]}
    index = VectorIndex(client or AsyncQdrantClient(location=":memory:"), HashEmbedder())
    cat = Catalog(
        index,
        store,
        lambda: rules,
        lambda: TargetCache(),
        registry=None,
        config=config,
        descriptions=Descriptions(tmp_path / "descriptions"),
        file_hashes=lambda: {"ng_offen": "abc", "licht": "def"},
        sleep=lambda _: asyncio.sleep(0),
    )
    cat._registry = REGISTRY  # noqa: SLF001
    return cat, store, index


def test_entity_doc_is_deterministic_german_text() -> None:
    args = ("light.aussen_hof", REGISTRY, _state("light.aussen_hof"), "zone.hof")
    a, b = entity_doc(*args), entity_doc(*args)
    assert a == b
    assert "Außenlicht" in a.text and "Bereich: Hof" in a.text and "Hue Outdoor" in a.text
    assert "zone.hof" in a.text
    assert a.payload["source"] == "generated" and a.payload["area"] == "Hof"
    assert set(a.payload) >= {"entity_id", "domain", "area", "device_id", "device_class",
                              "model_target", "description_hash", "source"}  # fmt: skip
    curated = entity_doc(*args, curated="Hauptlicht im Hof")
    assert curated.payload["source"] == "curated"
    assert curated.payload["description_hash"] != a.payload["description_hash"]


def test_rule_doc_and_entities() -> None:
    rules = {r.id: r for r in parse_rules_text(RULES)[0]}
    assert rule_entities(rules["licht"]) == {"light.aussen_hof"}
    doc = rule_doc(rules["ng_offen"], "abc")
    assert "Warne mich" in doc.text and "lock.nuki_ng" in doc.text
    assert doc.payload["file_hash"] == "abc" and doc.payload["target"] == "door.ng"
    assert doc.payload["entity_ids"] == ["door.ng", "lock.nuki_ng"]


async def test_default_selection_is_rule_entities_only(tmp_path: Path) -> None:
    cat, _, _ = await _catalog(tmp_path)
    assert {d.key for d in cat.entity_docs()} == {"lock.nuki_ng", "light.aussen_hof"}


async def test_allowlist_areas_exclude(tmp_path: Path) -> None:
    cfg = CatalogConfig(
        allowlist=["sensor.*"], exclude=["sensor.unbenutzt"], areas=["Nebengebäude"]
    )
    cat, _, _ = await _catalog(tmp_path, cfg)
    keys = {d.key for d in cat.entity_docs()}
    assert keys == {"lock.nuki_ng", "light.aussen_hof", "sensor.temp_bad"}


async def test_sync_embeds_only_changed_and_deletes_orphans(tmp_path: Path) -> None:
    cat, _, index = await _catalog(tmp_path)
    embedded: list[int] = []
    orig = index._embedder.passages  # noqa: SLF001

    async def counting(texts: list[str]) -> list[list[float]]:
        embedded.append(len(texts))
        return await orig(texts)

    index._embedder.passages = counting  # type: ignore[method-assign]  # noqa: SLF001
    assert await cat.sync()
    assert embedded == [2, 2]  # Entities, Regeln
    embedded.clear()
    await cat.sync()
    assert embedded == []  # nichts geändert
    cat._descriptions.set("light.aussen_hof", "Hauptlicht im Hof")  # noqa: SLF001
    await cat.sync()
    assert embedded == [1]
    # Regel verschwindet → verwaister Eintrag wird gelöscht
    cat._rules = lambda: {}  # noqa: SLF001
    await cat.sync()
    assert await index.count("rules") == 0
    assert await index.count("entities") == 0


async def test_search_with_filters_and_curated_override(tmp_path: Path) -> None:
    cat, _, _ = await _catalog(tmp_path)
    cat.set_description("light.aussen_hof", "Scheinwerfer vor der Garage")
    await cat.sync()
    hits = await cat.search_entities("Scheinwerfer Garage", limit=5)
    assert hits[0]["entity_id"] == "light.aussen_hof" and hits[0]["source"] == "curated"
    assert [h["entity_id"] for h in await cat.search_entities("Schloss", domain="lock")] == [
        "lock.nuki_ng"
    ]
    assert await cat.search_entities("Licht", area="Nebengebäude") == [] or all(
        h["area"] == "Nebengebäude" for h in await cat.search_entities("Licht", area="Nebengebäude")
    )
    rules = await cat.search_rules("Tür offen")
    assert rules[0]["rule_id"] == "ng_offen" and rules[0]["file_hash"] == "abc"


async def test_curated_descriptions_survive_reload(tmp_path: Path) -> None:
    d = Descriptions(tmp_path / "descriptions")
    d.set("lock.nuki_ng", "Tür zum Nebengebäude")
    assert Descriptions(tmp_path / "descriptions").get("lock.nuki_ng") == "Tür zum Nebengebäude"


async def test_qdrant_failure_is_degraded_not_fatal(tmp_path: Path) -> None:
    client = AsyncQdrantClient(location=":memory:")
    cat, _, _ = await _catalog(tmp_path, client=client)
    assert await cat.sync()
    assert await cat.check() == "ok"

    async def boom(*_: Any, **__: Any) -> Any:
        raise ConnectionError("qdrant weg")

    client.get_collections = boom  # type: ignore[method-assign]
    client.collection_exists = boom  # type: ignore[method-assign]
    client.query_points = boom  # type: ignore[method-assign]
    assert await cat.sync() is False  # kein Crash
    assert await cat.check() == "degraded"
    with pytest.raises(CatalogUnavailableError):
        await cat.search_entities("x")


def test_state_history_record_prune_and_replay() -> None:
    clock = [NOW]
    h = StateHistory(relevant=lambda e: e != "sensor.egal", retention_days=2, now=lambda: clock[0])
    h.record("a.x", "off", NOW - timedelta(days=3))  # wird gleich gepruned
    h.record("a.x", "on", NOW - timedelta(hours=5))
    h.record("a.x", "off", NOW - timedelta(hours=1))
    assert h.prune() == 1
    rows = h.changes(["a.x"], NOW - timedelta(hours=3), NOW)
    assert [(r[1], r[2]) for r in rows] == [("a.x", "on"), ("a.x", "off")]
    assert rows[0][0] == NOW - timedelta(hours=3)  # Anfangszustand am Fensterstart


async def test_state_history_listener_filters() -> None:
    h = StateHistory(relevant=lambda e: e != "sensor.egal")
    for eid in ("a.x", "sensor.egal"):
        await h.on_change(StateChange(eid, None, _state(eid)))
    await h.on_change(StateChange("a.x", _state("a.x"), _state("a.x", "on")))  # nur Attribut
    rows = h.changes(["a.x", "sensor.egal"], NOW - timedelta(days=1), NOW + timedelta(days=1))
    assert [(r[1], r[2]) for r in rows] == [("a.x", "on")]


async def test_e2e_against_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rules = tmp_path / "config" / "rules"
    rules.mkdir(parents=True)
    (rules / "r.yaml").write_text(
        "rules:\n- id: regen\n  triggers: [{type: state, entity_id: sensor.regen_rate, to: '1'}]\n"
        "  actions: [{type: ha_service, service: light.turn_on,"
        " target: {entity_id: light.aussen_hof}}]\n"
    )
    async with running_mock("tok") as mock:
        for k, v in {
            "DATA_DIR": str(tmp_path / "data"),
            "CONFIG_DIR": str(tmp_path / "config"),
            "HA_URL": mock.ws_url,
            "HA_TOKEN": "tok",
            "QDRANT_URL": ":memory:",
            "EMBED_MODEL": "hash",
        }.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("HA_MCP_URL", raising=False)
        monkeypatch.delenv("MQTT_HOST", raising=False)
        app = app_factory()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
        ):
            async with asyncio.timeout(10):
                while True:
                    body = (await c.get("/readyz")).json()
                    if body["checks"].get("qdrant") == "ok":
                        break
                    await asyncio.sleep(0.05)
            assert body["status"] == "ok"
            await mock.ha.set_state("sensor.regen_rate", "1")
            await asyncio.sleep(0.3)
