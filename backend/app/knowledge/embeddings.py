from __future__ import annotations

import hashlib
import asyncio
import math
import re
from typing import Protocol

from openai import AsyncOpenAI

from backend.app.config import Settings
from backend.app.security.circuit_breaker import CircuitBreaker, optional_guard


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    async def embed(
        self, texts: list[str], *, is_query: bool = False
    ) -> list[list[float]]: ...
    async def close(self) -> None: ...


_ASCII_WORD = re.compile(r"[a-zA-Z0-9_-]+")
_SYNONYM_GROUPS = (
    ("退货", "退款", "七天无理由", "不想要"),
    ("物流", "快递", "配送", "运输", "到货"),
    ("破损", "损坏", "碎了", "漏液"),
    ("保修", "维修", "故障", "坏了"),
    ("发票", "开票", "红冲"),
    ("价保", "降价", "差价"),
    ("拒收", "签收", "验货"),
    ("生鲜", "食品", "变质"),
)


def _tokens(text: str) -> list[str]:
    normalized = "".join(text.casefold().split())
    han = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    tokens = han + ["".join(han[index : index + 2]) for index in range(len(han) - 1)]
    tokens.extend(_ASCII_WORD.findall(normalized))
    for group in _SYNONYM_GROUPS:
        if any(term in normalized for term in group):
            tokens.extend(group)
    return tokens or [normalized]


class HashEmbeddingProvider:
    """Dependency-free deterministic vectorizer for development and tests."""

    name = "local-hash-zh-v1"

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    async def embed(
        self, texts: list[str], *, is_query: bool = False
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in _tokens(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
                index = int.from_bytes(digest[:8], "big") % self.dimensions
                sign = 1.0 if digest[8] & 1 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors

    async def close(self) -> None:
        return None


class OpenAIEmbeddingProvider:
    def __init__(self, settings: Settings, breaker: CircuitBreaker | None = None) -> None:
        if not settings.embedding_api_key:
            raise RuntimeError("EMBEDDING_API_KEY is required when EMBEDDING_MODE=openai")
        self.name = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self.breaker = breaker
        self.client = AsyncOpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            timeout=settings.agent_timeout_seconds,
            max_retries=0,
        )

    async def embed(
        self, texts: list[str], *, is_query: bool = False
    ) -> list[list[float]]:
        async with optional_guard(self.breaker):
            response = await self.client.embeddings.create(
                model=self.name,
                input=texts,
                dimensions=self.dimensions,
            )
        vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
        if len(vectors) != len(texts) or any(len(vector) != self.dimensions for vector in vectors):
            raise RuntimeError("Embedding provider returned an invalid shape")
        return vectors

    async def close(self) -> None:
        await self.client.close()


class BgeSmallZhEmbeddingProvider:
    query_instruction = "为这个句子生成表示以用于检索相关文章："

    def __init__(self, settings: Settings) -> None:
        self.name = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self.batch_size = settings.embedding_batch_size
        self._model = None
        self._load_lock = asyncio.Lock()

    async def _get_model(self):
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise RuntimeError(
                        "sentence-transformers is required for EMBEDDING_MODE=bge"
                    ) from exc
                def load_model():
                    try:
                        return SentenceTransformer(
                            self.name, device="cpu", local_files_only=True
                        )
                    except OSError:
                        return SentenceTransformer(self.name, device="cpu")

                self._model = await asyncio.to_thread(load_model)
                dimension_getter = getattr(
                    self._model,
                    "get_embedding_dimension",
                    None,
                )
                if dimension_getter is None:
                    dimension_getter = self._model.get_sentence_embedding_dimension
                actual_dimensions = dimension_getter()
                if actual_dimensions != self.dimensions:
                    raise RuntimeError(
                        f"Embedding dimension mismatch: expected {self.dimensions}, "
                        f"got {actual_dimensions}"
                    )
        return self._model

    async def embed(
        self, texts: list[str], *, is_query: bool = False
    ) -> list[list[float]]:
        model = await self._get_model()
        inputs = (
            [self.query_instruction + text for text in texts]
            if is_query
            else texts
        )

        def encode() -> list[list[float]]:
            vectors = model.encode(
                inputs,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            return vectors.tolist()

        return await asyncio.to_thread(encode)

    async def close(self) -> None:
        self._model = None


def create_embedding_provider(
    settings: Settings, breaker: CircuitBreaker | None = None
) -> EmbeddingProvider:
    if settings.embedding_dimensions != 512:
        raise ValueError("EMBEDDING_DIMENSIONS must be 512 for the current database schema")
    if settings.embedding_mode == "bge":
        return BgeSmallZhEmbeddingProvider(settings)
    if settings.embedding_mode == "openai":
        return OpenAIEmbeddingProvider(settings, breaker)
    return HashEmbeddingProvider(settings.embedding_dimensions)
