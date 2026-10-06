from __future__ import annotations

import asyncio
import math
from typing import Protocol

from backend.app.config import Settings


class Reranker(Protocol):
    name: str

    async def score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]: ...
    async def warmup(self) -> None: ...
    async def close(self) -> None: ...


class BgeReranker:
    """Local cross-encoder reranker with calibrated sigmoid scores."""

    def __init__(self, settings: Settings) -> None:
        self.name = settings.reranker_model
        self.batch_size = settings.reranker_batch_size
        self.max_length = settings.reranker_max_length
        self._tokenizer = None
        self._model = None
        self._torch = None
        self._load_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()

    async def _get_model(self):
        if self._model is not None:
            return self._tokenizer, self._model, self._torch
        async with self._load_lock:
            if self._model is None:
                try:
                    import torch
                    from transformers import AutoModelForSequenceClassification, AutoTokenizer
                except ImportError as exc:
                    raise RuntimeError(
                        "torch and transformers are required when RERANKER_ENABLED=true"
                    ) from exc

                def load_model():
                    try:
                        tokenizer = AutoTokenizer.from_pretrained(
                            self.name,
                            local_files_only=True,
                        )
                        model = AutoModelForSequenceClassification.from_pretrained(
                            self.name,
                            local_files_only=True,
                        )
                    except OSError:
                        tokenizer = AutoTokenizer.from_pretrained(self.name)
                        model = AutoModelForSequenceClassification.from_pretrained(self.name)
                    model.eval()
                    model.to("cpu")
                    return tokenizer, model

                self._tokenizer, self._model = await asyncio.to_thread(load_model)
                self._torch = torch
        return self._tokenizer, self._model, self._torch

    async def score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        tokenizer, model, torch = await self._get_model()

        def infer() -> list[float]:
            scores: list[float] = []
            with torch.inference_mode():
                for start in range(0, len(pairs), self.batch_size):
                    batch = pairs[start : start + self.batch_size]
                    queries = [query for query, _ in batch]
                    documents = [document for _, document in batch]
                    inputs = tokenizer(
                        queries,
                        documents,
                        padding=True,
                        truncation=True,
                        max_length=self.max_length,
                        return_tensors="pt",
                    )
                    logits = model(**inputs, return_dict=True).logits.reshape(-1)
                    for raw_value in logits.cpu().tolist():
                        value = float(raw_value)
                        if value >= 0:
                            scores.append(1.0 / (1.0 + math.exp(-value)))
                        else:
                            exp_value = math.exp(value)
                            scores.append(exp_value / (1.0 + exp_value))
            return scores

        # PyTorch CPU inference on one shared model is serialized deliberately.
        async with self._inference_lock:
            result = await asyncio.to_thread(infer)
        if len(result) != len(pairs):
            raise RuntimeError("Reranker returned an invalid score count")
        return result

    async def warmup(self) -> None:
        await self.score_pairs([("售后政策", "商品质量问题可以申请售后处理。")])

    async def close(self) -> None:
        self._tokenizer = None
        self._model = None
        self._torch = None


def create_reranker(settings: Settings) -> Reranker | None:
    # Hash embeddings are used by unit tests and deterministic smoke tests.
    if not settings.reranker_enabled or settings.embedding_mode == "hash":
        return None
    return BgeReranker(settings)
