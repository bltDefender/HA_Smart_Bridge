"""Embeddings lokal, offlinefähig: fastembed (multilingual-e5-small) oder `hash` für Dev/Tests."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from pathlib import Path
from typing import Protocol


class Embedder(Protocol):
    dim: int

    async def passages(self, texts: list[str]) -> list[list[float]]: ...

    async def query(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Deterministisch, ohne Modell: Wort- und Zeichen-Trigramm-Hashes. Nur Dev/Tests (`hash`)."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in re.findall(r"\w+", text.lower()):
            grams = [word, *(word[i : i + 3] for i in range(max(len(word) - 2, 0)))]
            for gram in grams:
                h = int.from_bytes(hashlib.sha256(gram.encode()).digest()[:8], "big")
                vec[h % self.dim] += 1.0 if gram == word else 0.5
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def query(self, text: str) -> list[float]:
        return self._vec(text)


class FastEmbedder:
    """fastembed/ONNX. `multilingual-e5-small` ist dort nicht vordefiniert und wird als eigenes
    Modell registriert. Cache-Verzeichnis im Image oder Volume, danach offline nutzbar."""

    def __init__(self, model: str, cache_dir: Path | None = None) -> None:
        self.model_name, self._cache = model, cache_dir
        self.dim = 384
        self._model: object | None = None
        self._e5 = "e5" in model.lower()

    def _load(self) -> object:
        if self._model is None:
            from fastembed import TextEmbedding
            from fastembed.common.model_description import ModelSource, PoolingType

            known = {m["model"] for m in TextEmbedding.list_supported_models()}
            if self.model_name not in known:
                TextEmbedding.add_custom_model(
                    model=self.model_name,
                    pooling=PoolingType.MEAN,
                    normalization=True,
                    sources=ModelSource(hf=self.model_name),
                    dim=self.dim,
                    model_file="onnx/model.onnx",
                )
            self._model = TextEmbedding(
                self.model_name, cache_dir=str(self._cache) if self._cache else None
            )
        return self._model

    def _embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return [[float(x) for x in v] for v in model.embed(texts)]  # type: ignore[attr-defined]

    async def passages(self, texts: list[str]) -> list[list[float]]:
        prefix = "passage: " if self._e5 else ""
        return await asyncio.to_thread(self._embed, [prefix + t for t in texts])

    async def query(self, text: str) -> list[float]:
        prefix = "query: " if self._e5 else ""
        return (await asyncio.to_thread(self._embed, [prefix + text]))[0]


def make_embedder(model: str, cache_dir: Path | None) -> Embedder:
    return HashEmbedder() if model == "hash" else FastEmbedder(model, cache_dir)


if __name__ == "__main__":  # Docker-Build: Modell ins Image cachen
    import os

    FastEmbedder(
        os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-small"),
        Path(os.environ["EMBED_CACHE_DIR"]) if os.environ.get("EMBED_CACHE_DIR") else None,
    )._embed(["warmup"])
