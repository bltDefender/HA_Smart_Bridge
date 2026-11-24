"""Qdrant-Zugriff: Collections `entities` und `rules`, Sync per `description_hash`."""

from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from .documents import Doc
from .embedder import Embedder

ENTITIES = "entities"
RULES = "rules"
_NS = uuid.UUID("6f1c1f0e-0a5a-4c55-9a52-5b1f0f0e8b11")
_BATCH = 64


def point_id(collection: str, key: str) -> str:
    return str(uuid.uuid5(_NS, f"{collection}:{key}"))


class VectorIndex:
    def __init__(self, client: AsyncQdrantClient, embedder: Embedder) -> None:
        self._client, self._embedder = client, embedder

    async def ping(self) -> None:
        await self._client.get_collections()

    async def ensure(self, collection: str) -> None:
        if await self._client.collection_exists(collection):
            info = await self._client.get_collection(collection)
            params = info.config.params.vectors
            if isinstance(params, models.VectorParams) and params.size == self._embedder.dim:
                return
            await self._client.delete_collection(collection)  # anderes Modell → neu aufbauen
        await self._client.create_collection(
            collection,
            vectors_config=models.VectorParams(
                size=self._embedder.dim, distance=models.Distance.COSINE
            ),
        )

    async def _existing(self, collection: str) -> dict[str, tuple[str, str]]:
        """`point_id -> (key, description_hash)`."""
        found: dict[str, tuple[str, str]] = {}
        offset: Any = None
        while True:
            points, offset = await self._client.scroll(
                collection, limit=256, offset=offset, with_payload=["key", "description_hash"]
            )
            for p in points:
                payload = p.payload or {}
                found[str(p.id)] = (str(payload.get("key")), str(payload.get("description_hash")))
            if offset is None:
                return found

    async def sync(self, collection: str, docs: list[Doc]) -> tuple[int, int]:
        """Nur geänderte Dokumente neu einbetten, Verwaistes löschen. → (upserts, deletes)."""
        await self.ensure(collection)
        existing = await self._existing(collection)
        wanted = {point_id(collection, d.key): d for d in docs}
        changed = [
            (pid, d)
            for pid, d in wanted.items()
            if existing.get(pid, ("", ""))[1] != d.payload["description_hash"]
        ]
        for i in range(0, len(changed), _BATCH):
            chunk = changed[i : i + _BATCH]
            vectors = await self._embedder.passages([d.text for _, d in chunk])
            await self._client.upsert(
                collection,
                [
                    models.PointStruct(id=pid, vector=vec, payload={**d.payload, "key": d.key})
                    for (pid, d), vec in zip(chunk, vectors, strict=True)
                ],
            )
        orphans = [pid for pid in existing if pid not in wanted]
        if orphans:
            await self._client.delete(collection, models.PointIdsList(points=list(orphans)))
        return len(changed), len(orphans)

    async def search(
        self,
        collection: str,
        query: str,
        limit: int = 10,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        vector = await self._embedder.query(query)
        flt = (
            models.Filter(
                must=[
                    models.FieldCondition(key=k, match=models.MatchValue(value=v))
                    for k, v in filters.items()
                ]
            )
            if filters
            else None
        )
        res = await self._client.query_points(
            collection, query=vector, limit=limit, query_filter=flt, with_payload=True
        )
        return [{**(p.payload or {}), "score": p.score} for p in res.points]

    async def count(self, collection: str) -> int:
        return (await self._client.count(collection)).count
