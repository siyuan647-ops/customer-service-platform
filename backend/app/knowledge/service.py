from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import any_, delete, exists, func, literal, or_, select, text

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.models import KnowledgeChunk, KnowledgeDocument
from backend.app.storage import ObjectStorage
from backend.app.knowledge.bm25 import bm25_scores
from backend.app.knowledge.embeddings import EmbeddingProvider
from backend.app.knowledge.parser import parse_document, split_document
from backend.app.knowledge.reranker import Reranker


class KnowledgeValidationError(ValueError):
    pass


@dataclass(slots=True)
class IngestResult:
    document_id: uuid.UUID
    title: str
    source_filename: str
    product_categories: list[str]
    chunk_count: int
    unchanged: bool


_INTENT_QUERY_TERM = {
    "refund": "退款",
    "shipping": "物流",
    "after_sale": "售后",
}


class KnowledgeService:
    def __init__(
        self,
        *,
        database: Database,
        storage: ObjectStorage,
        embeddings: EmbeddingProvider,
        settings: Settings,
        reranker: Reranker | None = None,
    ) -> None:
        self.database = database
        self.storage = storage
        self.embeddings = embeddings
        self.settings = settings
        self.reranker = reranker

    async def close(self) -> None:
        await self.embeddings.close()
        if self.reranker is not None:
            await self.reranker.close()

    async def warmup(self) -> None:
        await self.embeddings.embed(["知识库检索模型预热"], is_query=True)
        if self.reranker is not None and self.settings.reranker_warmup_on_startup:
            await self.reranker.warmup()

    async def ping(self) -> bool:
        async with self.database.engine.connect() as connection:
            await connection.execute(text("SELECT 1 FROM knowledge_documents LIMIT 1"))
        return True

    async def ingest(
        self, *, filename: str, data: bytes, content_type: str = "text/markdown"
    ) -> IngestResult:
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix.casefold()
        if not safe_name or suffix not in {".md", ".markdown", ".txt"}:
            raise KnowledgeValidationError("仅支持 UTF-8 Markdown 或 TXT 政策文件")
        if not data:
            raise KnowledgeValidationError("上传文件不能为空")
        if len(data) > self.settings.knowledge_upload_max_bytes:
            raise KnowledgeValidationError("上传文件超过大小限制")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise KnowledgeValidationError("政策文件必须使用 UTF-8 编码") from exc

        parsed = parse_document(text)
        parsed_chunks = split_document(
            parsed,
            max_chars=self.settings.knowledge_chunk_chars,
            overlap_chars=self.settings.knowledge_chunk_overlap,
        )
        content_hash = hashlib.sha256(parsed.cleaned_text.encode("utf-8")).hexdigest()
        document_id = uuid.uuid5(uuid.NAMESPACE_URL, f"policy:{safe_name}")

        async with self.database.session_factory() as session:
            existing = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_filename == safe_name
                )
            )
            if (
                existing is not None
                and existing.content_hash == content_hash
                and existing.embedding_model == self.embeddings.name
            ):
                chunk_count = len(
                    list(
                        await session.scalars(
                            select(KnowledgeChunk.id).where(
                                KnowledgeChunk.document_id == existing.id
                            )
                        )
                    )
                )
                if chunk_count > 0:
                    return IngestResult(
                        existing.id,
                        existing.title,
                        existing.source_filename,
                        existing.product_categories,
                        chunk_count,
                        True,
                    )

        embedding_inputs = [
            f"政策：{parsed.title}\n章节：{chunk.section}\n{chunk.content}"
            for chunk in parsed_chunks
        ]
        vectors: list[list[float]] = []
        batch_size = self.settings.embedding_batch_size
        for start in range(0, len(embedding_inputs), batch_size):
            vectors.extend(
                await self.embeddings.embed(
                    embedding_inputs[start : start + batch_size], is_query=False
                )
            )
        if len(vectors) != len(parsed_chunks):
            raise RuntimeError("Embedding count does not match chunk count")

        object_key = f"knowledge/{document_id}/{content_hash}{suffix}"
        source_key = await self.storage.put_bytes(object_key, data, content_type)

        async with self.database.session_factory() as session:
            document = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_filename == safe_name
                )
            )
            if document is None:
                document = KnowledgeDocument(id=document_id, source_filename=safe_name)
                session.add(document)
            else:
                document_id = document.id
                await session.execute(
                    delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
                )

            document.title = parsed.title
            document.source_key = source_key
            document.product_categories = parsed.product_categories
            document.content_hash = content_hash
            document.raw_text = parsed.cleaned_text
            document.document_metadata = parsed.metadata
            document.embedding_model = self.embeddings.name
            document.status = "ready"
            for index, (chunk, vector) in enumerate(zip(parsed_chunks, vectors, strict=True)):
                session.add(
                    KnowledgeChunk(
                        document_id=document_id,
                        chunk_index=index,
                        section=chunk.section,
                        content=chunk.content,
                        content_hash=hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                        embedding=vector,
                    )
                )
            await session.commit()

        return IngestResult(
            document_id,
            parsed.title,
            safe_name,
            parsed.product_categories,
            len(parsed_chunks),
            False,
        )

    async def list_documents(self) -> list[KnowledgeDocument]:
        async with self.database.session_factory() as session:
            rows = await session.scalars(
                select(KnowledgeDocument).order_by(KnowledgeDocument.title)
            )
            return list(rows)

    async def get_document(self, document_id: uuid.UUID) -> KnowledgeDocument | None:
        async with self.database.session_factory() as session:
            return await session.get(KnowledgeDocument, document_id)

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        policy_category: str = "general",
        product_category: str | None = None,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if len(query) < 2:
            raise KnowledgeValidationError("检索问题至少需要两个字符")
        query_vector = (await self.embeddings.embed([query], is_query=True))[0]
        candidate_limit = max(
            top_k * 4,
            self.settings.knowledge_retrieval_candidates,
            self.settings.reranker_candidate_limit if self.reranker is not None else 0,
        )

        async with self.database.session_factory() as session:
            base_filter = []
            if product_category:
                if self.database.engine.dialect.name == "postgresql":
                    base_filter.append(
                        or_(
                            literal(product_category)
                            == any_(KnowledgeDocument.product_categories),
                            literal("全品类")
                            == any_(KnowledgeDocument.product_categories),
                        )
                    )
                else:
                    categories = func.json_each(
                        KnowledgeDocument.product_categories
                    ).table_valued("key", "value")
                    base_filter.append(
                        exists(
                            select(1)
                            .select_from(categories)
                            .where(
                                categories.c.value.in_(
                                    (product_category, "全品类")
                                )
                            )
                        )
                    )
            if self.database.engine.dialect.name == "postgresql":
                distance = KnowledgeChunk.embedding.cosine_distance(query_vector).label(
                    "distance"
                )
                statement = (
                    select(KnowledgeChunk, KnowledgeDocument, distance)
                    .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                    .where(*base_filter)
                    .order_by(distance)
                    .limit(candidate_limit)
                )
                vector_rows = list((await session.execute(statement)).all())
            else:
                statement = (
                    select(KnowledgeChunk, KnowledgeDocument)
                    .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                    .where(*base_filter)
                )
                rows = list((await session.execute(statement)).all())
                vector_rows = [
                    (chunk, document, 1.0 - _cosine(query_vector, list(chunk.embedding)))
                    for chunk, document in rows
                ]
                vector_rows.sort(key=lambda row: row[2])
                vector_rows = vector_rows[:candidate_limit]

            bm25_statement = (
                select(KnowledgeChunk, KnowledgeDocument)
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                .where(*base_filter)
            )
            bm25_candidates = list((await session.execute(bm25_statement)).all())

        intent_term = _INTENT_QUERY_TERM.get(policy_category, "")
        bm25_query = f"{query} {intent_term}" if intent_term else query
        searchable_documents = [
            "\n".join(
                (
                    document.title,
                    " ".join(document.product_categories),
                    chunk.section,
                    chunk.content,
                )
            )
            for chunk, document in bm25_candidates
        ]
        scores = bm25_scores(
            bm25_query,
            searchable_documents,
            k1=self.settings.knowledge_bm25_k1,
            b=self.settings.knowledge_bm25_b,
        )
        bm25_rows = sorted(
            (
                (chunk, document, score)
                for (chunk, document), score in zip(
                    bm25_candidates, scores, strict=True
                )
                if score > 0
            ),
            key=lambda row: row[2],
            reverse=True,
        )[:candidate_limit]

        combined: dict[uuid.UUID, dict[str, Any]] = {}
        for rank, (chunk, document, distance) in enumerate(vector_rows, start=1):
            combined[chunk.id] = {
                "chunk": chunk,
                "document": document,
                "vector_score": max(-1.0, min(1.0, 1.0 - float(distance))),
                "bm25_score": 0.0,
                "vector_rank": rank,
                "bm25_rank": None,
                "rrf": 1.0 / (self.settings.knowledge_rrf_k + rank),
            }
        for rank, (chunk, document, bm25_score) in enumerate(bm25_rows, start=1):
            item = combined.setdefault(
                chunk.id,
                {
                    "chunk": chunk,
                    "document": document,
                    "vector_score": 0.0,
                    "bm25_score": 0.0,
                    "vector_rank": None,
                    "bm25_rank": None,
                    "rrf": 0.0,
                },
            )
            item["bm25_score"] = bm25_score
            item["bm25_rank"] = rank
            item["rrf"] += 1.0 / (self.settings.knowledge_rrf_k + rank)

        ranked = sorted(
            combined.values(),
            key=lambda item: (
                item["rrf"],
                item["vector_rank"] is not None and item["bm25_rank"] is not None,
                item["vector_score"],
                item["bm25_score"],
            ),
            reverse=True,
        )
        if self.reranker is not None:
            rerank_candidates = ranked[: self.settings.reranker_candidate_limit]
            pairs = [
                (
                    query,
                    "\n".join(
                        (
                            item["document"].title,
                            item["chunk"].section,
                            item["chunk"].content,
                        )
                    ),
                )
                for item in rerank_candidates
            ]
            rerank_scores = await self.reranker.score_pairs(pairs)
            for item, score in zip(rerank_candidates, rerank_scores, strict=True):
                item["rerank_score"] = score
            ranked = sorted(
                (
                    item
                    for item in rerank_candidates
                    if item["rerank_score"] >= self.settings.reranker_threshold
                ),
                key=lambda item: (item["rerank_score"], item["rrf"]),
                reverse=True,
            )
        else:
            for item in ranked:
                item["rerank_score"] = None
        ranked = ranked[:top_k]
        return [self._serialize_result(item) for item in ranked]

    @staticmethod
    def _serialize_result(item: dict[str, Any]) -> dict[str, Any]:
        chunk: KnowledgeChunk = item["chunk"]
        document: KnowledgeDocument = item["document"]
        return {
            "document_id": str(document.id),
            "chunk_id": str(chunk.id),
            "title": document.title,
            "section": chunk.section,
            "content": chunk.content,
            "product_categories": document.product_categories,
            "score": round(float(item["rrf"]), 6),
            "vector_score": round(float(item["vector_score"]), 6),
            "bm25_score": round(float(item["bm25_score"]), 6),
            "vector_rank": item["vector_rank"],
            "bm25_rank": item["bm25_rank"],
            "rerank_score": (
                round(float(item["rerank_score"]), 6)
                if item.get("rerank_score") is not None
                else None
            ),
            "citation": {
                "source_filename": document.source_filename,
                "title": document.title,
                "section": chunk.section,
                "chunk_index": chunk.chunk_index,
            },
        }


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right)) or 1.0
    return numerator / (left_norm * right_norm)
