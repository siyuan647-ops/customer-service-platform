from __future__ import annotations

import secrets
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request, Response

from backend.app.schemas import (
    AdminAfterSalesCaseRead,
    AdminAfterSalesEvidenceRead,
    AfterSalesApprovalRequest,
    AfterSalesCaseRead,
    AfterSalesRejectionRequest,
)
from backend.app.services.after_sales_cases import (
    AfterSalesCaseConflict,
    AfterSalesCaseNotFound,
    AfterSalesCaseValidationError,
    AfterSalesDependencyError,
)


router = APIRouter(prefix="/admin/after-sales", tags=["after-sales-admin"])


def _require_admin(request: Request, token: str | None) -> None:
    configured = request.app.state.settings.after_sales_admin_token
    if not configured and request.app.state.settings.app_env == "production":
        raise HTTPException(
            status_code=503,
            detail="AFTER_SALES_ADMIN_TOKEN is not configured",
        )
    if configured and (token is None or not secrets.compare_digest(configured, token)):
        raise HTTPException(status_code=403, detail="After-sales admin access denied")


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, AfterSalesCaseNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, AfterSalesCaseConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, AfterSalesDependencyError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _to_admin_read(request: Request, case) -> AdminAfterSalesCaseRead:
    amount, currency = await request.app.state.after_sales_execution.get_order_amount(case)
    base = AfterSalesCaseRead.model_validate(case)
    payload = base.model_dump()
    payload["evidence"] = [
        AdminAfterSalesEvidenceRead.model_validate(item).model_dump()
        for item in case.evidence
    ]
    return AdminAfterSalesCaseRead(
        **payload,
        max_refund_amount=amount,
        currency=currency,
    )


@router.get("/cases", response_model=list[AdminAfterSalesCaseRead])
async def list_cases(
    request: Request,
    status: str | None = None,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> list[AdminAfterSalesCaseRead]:
    _require_admin(request, admin_token)
    cases = await request.app.state.after_sales_execution.list_cases(status)
    return [await _to_admin_read(request, case) for case in cases]


@router.get("/cases/{case_no}", response_model=AdminAfterSalesCaseRead)
async def get_case(
    case_no: str,
    request: Request,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> AdminAfterSalesCaseRead:
    _require_admin(request, admin_token)
    try:
        case = await request.app.state.after_sales_execution.get_case(case_no)
        return await _to_admin_read(request, case)
    except (
        AfterSalesCaseNotFound,
        AfterSalesDependencyError,
    ) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.post("/cases/{case_no}/start-review", response_model=AdminAfterSalesCaseRead)
async def start_review(
    case_no: str,
    request: Request,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    reviewer_id: str = Header(default="demo-agent", alias="X-Admin-ID"),
) -> AdminAfterSalesCaseRead:
    _require_admin(request, admin_token)
    try:
        case = await request.app.state.after_sales_execution.start_review(
            case_no, reviewer_id
        )
        return await _to_admin_read(request, case)
    except (AfterSalesCaseNotFound, AfterSalesCaseConflict) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.post("/cases/{case_no}/approve", response_model=AdminAfterSalesCaseRead)
async def approve_case(
    case_no: str,
    payload: AfterSalesApprovalRequest,
    request: Request,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    reviewer_id: str = Header(default="demo-agent", alias="X-Admin-ID"),
) -> AdminAfterSalesCaseRead:
    _require_admin(request, admin_token)
    try:
        case = await request.app.state.after_sales_execution.approve(
            case_no=case_no,
            reviewer_id=reviewer_id,
            action=payload.action,
            refund_amount=payload.refund_amount,
            reason=payload.reason,
        )
        return await _to_admin_read(request, case)
    except (
        AfterSalesCaseNotFound,
        AfterSalesCaseConflict,
        AfterSalesCaseValidationError,
        AfterSalesDependencyError,
    ) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.post("/cases/{case_no}/reject", response_model=AdminAfterSalesCaseRead)
async def reject_case(
    case_no: str,
    payload: AfterSalesRejectionRequest,
    request: Request,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    reviewer_id: str = Header(default="demo-agent", alias="X-Admin-ID"),
) -> AdminAfterSalesCaseRead:
    _require_admin(request, admin_token)
    try:
        case = await request.app.state.after_sales_execution.reject(
            case_no=case_no,
            reviewer_id=reviewer_id,
            reason=payload.reason,
        )
        return await _to_admin_read(request, case)
    except (AfterSalesCaseNotFound, AfterSalesCaseConflict) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.post("/cases/{case_no}/retry", response_model=AdminAfterSalesCaseRead)
async def retry_case(
    case_no: str,
    request: Request,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    reviewer_id: str = Header(default="demo-agent", alias="X-Admin-ID"),
) -> AdminAfterSalesCaseRead:
    _require_admin(request, admin_token)
    try:
        case = await request.app.state.after_sales_execution.retry(case_no, reviewer_id)
        return await _to_admin_read(request, case)
    except (AfterSalesCaseNotFound, AfterSalesCaseConflict) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.get("/evidence/{evidence_id}")
async def download_evidence(
    evidence_id: uuid.UUID,
    request: Request,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> Response:
    _require_admin(request, admin_token)
    try:
        evidence = await request.app.state.after_sales_execution.get_evidence(evidence_id)
        data = await request.app.state.storage.get_bytes(evidence.object_key)
    except (AfterSalesCaseNotFound, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type=evidence.content_type,
        headers={
            "Content-Disposition": (
                "inline; filename=\"evidence\"; "
                f"filename*=UTF-8''{quote(evidence.filename, safe='')}"
            )
        },
    )
