from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from backend.app.security.sessions import require_customer

from backend.app.schemas import (
    AfterSalesCaseCreate,
    AfterSalesCaseMaterialUpdate,
    AfterSalesCaseRead,
    AfterSalesEvidenceRead,
)
from backend.app.services.after_sales_cases import (
    AfterSalesCaseConflict,
    AfterSalesCaseNotFound,
    AfterSalesCaseValidationError,
    AfterSalesDependencyError,
)


router = APIRouter(prefix="/after-sales/cases", tags=["after-sales"])


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, AfterSalesCaseNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, AfterSalesCaseConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, AfterSalesDependencyError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("", response_model=AfterSalesCaseRead, status_code=status.HTTP_201_CREATED)
async def create_case(
    payload: AfterSalesCaseCreate,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> AfterSalesCaseRead:
    try:
        case = await request.app.state.after_sales_cases.create_case(
            customer_id=customer_id,
            order_no=payload.order_id,
            order_item_no=payload.order_item_id,
            case_type=payload.case_type,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        )
    except (
        AfterSalesCaseNotFound,
        AfterSalesCaseConflict,
        AfterSalesCaseValidationError,
        AfterSalesDependencyError,
    ) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")
    return AfterSalesCaseRead.model_validate(case)


@router.get("", response_model=list[AfterSalesCaseRead])
async def list_cases(
    request: Request,
    conversation_id: uuid.UUID | None = None,
    customer_id: uuid.UUID = Depends(require_customer),
) -> list[AfterSalesCaseRead]:
    cases = await request.app.state.after_sales_cases.list_cases(
        customer_id,
        conversation_id=conversation_id,
    )
    return [AfterSalesCaseRead.model_validate(case) for case in cases]


@router.get("/{case_no}", response_model=AfterSalesCaseRead)
async def get_case(
    case_no: str,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> AfterSalesCaseRead:
    try:
        case = await request.app.state.after_sales_cases.get_case(customer_id, case_no)
    except AfterSalesCaseNotFound as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")
    return AfterSalesCaseRead.model_validate(case)


@router.patch("/{case_no}/materials", response_model=AfterSalesCaseRead)
async def update_materials(
    case_no: str,
    payload: AfterSalesCaseMaterialUpdate,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> AfterSalesCaseRead:
    try:
        case = await request.app.state.after_sales_cases.update_materials(
            customer_id=customer_id,
            case_no=case_no,
            case_type=payload.case_type,
            problem_type=payload.problem_type,
            problem_discovered_at=payload.problem_discovered_at,
            problem_description=payload.problem_description,
        )
    except (
        AfterSalesCaseNotFound,
        AfterSalesCaseConflict,
        AfterSalesCaseValidationError,
    ) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")
    return AfterSalesCaseRead.model_validate(case)


@router.post(
    "/{case_no}/evidence",
    response_model=AfterSalesEvidenceRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_evidence(
    case_no: str,
    request: Request,
    file: UploadFile = File(),
    customer_id: uuid.UUID = Depends(require_customer),
) -> AfterSalesEvidenceRead:
    max_bytes = (
        request.app.state.settings.after_sales_video_upload_max_bytes
        if (file.content_type or "").startswith("video/")
        else request.app.state.settings.after_sales_upload_max_bytes
    )
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="Evidence file is too large")
    if not data:
        raise HTTPException(status_code=422, detail="Evidence file is empty")
    try:
        evidence = await request.app.state.after_sales_cases.add_evidence(
            customer_id=customer_id,
            case_no=case_no,
            filename=file.filename or "evidence",
            content_type=file.content_type or "application/octet-stream",
            data=data,
        )
    except (
        AfterSalesCaseNotFound,
        AfterSalesCaseConflict,
        AfterSalesCaseValidationError,
    ) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")
    return AfterSalesEvidenceRead.model_validate(evidence)


@router.post("/{case_no}/submit", response_model=AfterSalesCaseRead)
async def submit_case(
    case_no: str,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> AfterSalesCaseRead:
    try:
        case = await request.app.state.after_sales_cases.submit_case(customer_id, case_no)
    except (
        AfterSalesCaseNotFound,
        AfterSalesCaseConflict,
        AfterSalesCaseValidationError,
    ) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")
    return AfterSalesCaseRead.model_validate(case)
