from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile, status

from backend.app.knowledge.parser import DocumentParseError
from backend.app.knowledge.service import KnowledgeValidationError
from backend.app.schemas import (
    KnowledgeDocumentDetail,
    KnowledgeDocumentRead,
    KnowledgeIngestRead,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)


router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _require_admin(request: Request, token: str | None) -> None:
    configured = request.app.state.settings.knowledge_admin_token
    if not configured and request.app.state.settings.app_env == "production":
        raise HTTPException(status_code=503, detail="KNOWLEDGE_ADMIN_TOKEN is not configured")
    if configured and (token is None or not secrets.compare_digest(configured, token)):
        raise HTTPException(status_code=403, detail="Knowledge admin access denied")


@router.post(
    "/documents", response_model=KnowledgeIngestRead, status_code=status.HTTP_201_CREATED
)
async def upload_document(
    request: Request,
    file: UploadFile = File(),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> KnowledgeIngestRead:
    _require_admin(request, admin_token)
    data = await file.read(request.app.state.settings.knowledge_upload_max_bytes + 1)
    try:
        result = await request.app.state.knowledge.ingest(
            filename=file.filename or "policy.md",
            data=data,
            content_type=file.content_type or "text/markdown",
        )
    except (KnowledgeValidationError, DocumentParseError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return KnowledgeIngestRead.model_validate(result, from_attributes=True)


@router.get("/documents", response_model=list[KnowledgeDocumentRead])
async def list_documents(request: Request) -> list[KnowledgeDocumentRead]:
    documents = await request.app.state.knowledge.list_documents()
    return [KnowledgeDocumentRead.model_validate(item) for item in documents]


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentDetail)
async def get_document(document_id: uuid.UUID, request: Request) -> KnowledgeDocumentDetail:
    document = await request.app.state.knowledge.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return KnowledgeDocumentDetail.model_validate(document)


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    payload: KnowledgeSearchRequest, request: Request
) -> KnowledgeSearchResponse:
    results = await request.app.state.knowledge.search(
        payload.query,
        top_k=payload.top_k,
        policy_category=payload.policy_category,
        product_category=payload.product_category,
    )
    return KnowledgeSearchResponse(query=payload.query, results=results)
