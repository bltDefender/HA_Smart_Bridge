"""Katalog-Orchestrierung: Auswahl, Dokumente, Sync. Reine Lesehilfe, nie Teil der Auswertung."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..alerts.targets import TargetCache
from ..rules.models import Rule
from ..state import StateStore
from .documents import DOMAINS, Doc, Registry, entity_doc, rule_doc, rule_entities
from .index import ENTITIES, RULES, VectorIndex

log = logging.getLogger(__name__)

RegistryFetch = Callable[[], Awaitable[Registry]]
REGISTRY_EVENTS = frozenset(
    {"entity_registry_updated", "device_registry_updated", "area_registry_updated"}
)


class CatalogUnavailableError(Exception):
    """Qdrant/Embedding nicht erreichbar; Aufrufer melden `degraded`, nichts bricht ab."""


@dataclass(frozen=True)
class CatalogConfig:
    """`config/catalog.yaml`: ergänzt die Standardauswahl (Entities aus Regeln/Zielen/Aktionen)."""

    allowlist: list[str] = field(default_factory=list)  # Entity-Globs, z. B. `lock.*`
    exclude: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=lambda: list(DOMAINS))

    @classmethod
    def load(cls, path: Path) -> CatalogConfig:
        if not path.is_file():
            return cls()
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        known = {k: list(v) for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class Descriptions:
    """Kuratierte Texte (`config/descriptions/*.yaml`, Schlüssel `descriptions:`)."""

    WRITABLE = "mcp.yaml"

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._items: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self._items = {}
        if not self._dir.is_dir():
            return
        for path in sorted([*self._dir.glob("*.yaml"), *self._dir.glob("*.yml")]):
            data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
            self._items.update(
                {str(k): str(v) for k, v in (data.get("descriptions") or {}).items()}
            )

    def get(self, entity_id: str) -> str | None:
        return self._items.get(entity_id)

    def set(self, entity_id: str, text: str) -> None:
        path = self._dir / self.WRITABLE
        self._dir.mkdir(parents=True, exist_ok=True)
        yaml = YAML(typ="safe")
        data = (yaml.load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}
        data.setdefault("descriptions", {})[entity_id] = text
        yaml.default_flow_style = False
        with path.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
        self._items[entity_id] = text


async def fetch_registry(command: Callable[[dict[str, Any]], Awaitable[Any]]) -> Registry:
    entities = await command({"type": "config/entity_registry/list"})
    devices = await command({"type": "config/device_registry/list"})
    areas = await command({"type": "config/area_registry/list"})
    return Registry.from_lists(entities, devices, areas)


class Catalog:
    def __init__(
        self,
        index: VectorIndex,
        states: StateStore,
        rules: Callable[[], dict[str, Rule]],
        targets: Callable[[], TargetCache],
        *,
        registry: RegistryFetch | None = None,
        config: CatalogConfig | None = None,
        descriptions: Descriptions | None = None,
        file_hashes: Callable[[], dict[str, str]] = dict,
        debounce: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._index, self._states, self._rules, self._targets = index, states, rules, targets
        self._fetch, self._config = registry, config or CatalogConfig()
        self._descriptions = descriptions or Descriptions(Path("/nonexistent"))
        self._file_hashes, self._debounce, self._sleep = file_hashes, debounce, sleep
        self._registry = Registry()
        self._wanted = asyncio.Event()
        self.available = False
        self.selected: frozenset[str] = frozenset()

    # --- Auswahl und Dokumente ------------------------------------------------

    def _select(self, registry: Registry) -> set[str]:
        cfg = self._config
        known = set(registry.entities) | set(self._states.all())
        chosen: set[str] = set()
        for rule in self._rules().values():
            chosen |= rule_entities(rule)
        for target in self._targets().targets.values():
            chosen |= {e for e in target.get("entities") or [] if isinstance(e, str)}
        for eid in known:
            ent = registry.entities.get(eid, {})
            if (
                any(fnmatch.fnmatch(eid, g) for g in cfg.allowlist)
                or (cfg.areas and registry.area_of(eid) in cfg.areas)
                or (cfg.labels and set(ent.get("labels") or []) & set(cfg.labels))
            ):
                chosen.add(eid)
        return {
            e
            for e in chosen & known
            if e.split(".", 1)[0] in cfg.domains
            and not any(fnmatch.fnmatch(e, g) for g in cfg.exclude)
        }

    def entity_docs(self, registry: Registry | None = None) -> list[Doc]:
        registry = registry or self._registry
        target_of: dict[str, str] = {}
        for name, target in self._targets().targets.items():
            for eid in target.get("entities") or []:
                target_of.setdefault(str(eid), name)
        return [
            entity_doc(
                eid,
                registry,
                self._states.get(eid),
                target_of.get(eid),
                self._descriptions.get(eid),
            )
            for eid in sorted(self._select(registry))
        ]

    def rule_docs(self) -> list[Doc]:
        hashes = self._file_hashes()
        # per_entity-Kinder teilen sich die Datei ihrer Elternregel
        return [
            rule_doc(r, hashes.get(r.parent_id or r.id) or hashes.get(r.id))
            for r in self._rules().values()
        ]

    # --- Sync -------------------------------------------------------------------

    def area_of(self, entity_id: str) -> str | None:
        return self._registry.area_of(entity_id)

    def request_sync(self) -> None:
        self._wanted.set()

    async def run(self) -> None:
        """Worker: bündelt Sync-Anforderungen (Registry-Events, Regel-Reload, Zielantwort)."""
        while True:
            await self._wanted.wait()
            await self._sleep(self._debounce)
            self._wanted.clear()
            await self.sync()

    async def sync(self) -> bool:
        """Vollabgleich; re-embedded wird nur bei geänderter `description_hash`."""
        if self._fetch is not None:
            try:
                self._registry = await self._fetch()
            except Exception as exc:  # HA weg: mit dem letzten Stand weiterarbeiten
                log.warning("registry fetch failed: %r", exc)
        try:
            docs = self.entity_docs()
            self.selected = frozenset(d.key for d in docs)
            up, gone = await self._index.sync(ENTITIES, docs)
            r_up, r_gone = await self._index.sync(RULES, self.rule_docs())
        except Exception as exc:  # Qdrant/Embedding: nur degraded
            if self.available:
                log.warning("catalog sync failed: %r", exc)
            self.available = False
            return False
        self.available = True
        log.info("catalog synced: entities +%d -%d, rules +%d -%d", up, gone, r_up, r_gone)
        return True

    async def check(self) -> str:
        try:
            await self._index.ping()
        except Exception:
            self.available = False
            return "degraded"
        return "ok" if self.available else "degraded"

    # --- Abfragen ---------------------------------------------------------------

    async def _search(
        self, collection: str, query: str, limit: int, filters: dict[str, str]
    ) -> list[dict[str, Any]]:
        try:
            return await self._index.search(collection, query, limit, filters)
        except Exception as exc:
            self.available = False
            raise CatalogUnavailableError(repr(exc)) from exc

    async def search_entities(
        self, query: str, domain: str | None = None, area: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        filters = {k: v for k, v in (("domain", domain), ("area", area)) if v}
        return await self._search(ENTITIES, query, limit, filters)

    async def search_rules(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return await self._search(RULES, query, limit, {})

    def set_description(self, entity_id: str, text: str) -> None:
        self._descriptions.set(entity_id, text)
        self.request_sync()
