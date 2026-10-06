from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from backend.app.security.sessions import require_customer

from backend.app.schemas import (
    CustomerServiceRequestRead,
    InvoiceApplicationUpdate,
    ShippingAddressUpdate,
)
from backend.app.services.customer_operations import (
    CustomerOperationConflict,
    CustomerOperationDependencyError,
    CustomerOperationNotFound,
    CustomerOperationValidationError,
)


router = APIRouter(prefix="/service-requests", tags=["customer-operations"])


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, CustomerOperationNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, CustomerOperationConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, CustomerOperationDependencyError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=list[CustomerServiceRequestRead])
async def list_requests(
    request: Request,
    conversation_id: uuid.UUID | None = None,
    order_id: str | None = None,
    request_type: Literal[
        "address_change", "shipment_reminder", "invoice_application"
    ]
    | None = None,
    customer_id: uuid.UUID = Depends(require_customer),
) -> list[CustomerServiceRequestRead]:
    rows = await request.app.state.customer_operations.list(
        customer_id,
        conversation_id=conversation_id,
        order_no=order_id,
        request_type=request_type,
    )
    return [CustomerServiceRequestRead.model_validate(row) for row in rows]


@router.get("/{request_no}", response_model=CustomerServiceRequestRead)
async def get_request(
    request_no: str,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> CustomerServiceRequestRead:
    try:
        row = await request.app.state.customer_operations.get(customer_id, request_no)
        return CustomerServiceRequestRead.model_validate(row)
    except CustomerOperationNotFound as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.post("/{request_no}/address", response_model=CustomerServiceRequestRead)
async def submit_address(
    request_no: str,
    payload: ShippingAddressUpdate,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> CustomerServiceRequestRead:
    try:
        row = await request.app.state.customer_operations.submit_address(
            customer_id=customer_id,
            request_no=request_no,
            address=payload.model_dump(),
        )
        return CustomerServiceRequestRead.model_validate(row)
    except (
        CustomerOperationNotFound,
        CustomerOperationConflict,
        CustomerOperationValidationError,
        CustomerOperationDependencyError,
    ) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.post("/{request_no}/invoice", response_model=CustomerServiceRequestRead)
async def submit_invoice(
    request_no: str,
    payload: InvoiceApplicationUpdate,
    request: Request,
    customer_id: uuid.UUID = Depends(require_customer),
) -> CustomerServiceRequestRead:
    try:
        row = await request.app.state.customer_operations.submit_invoice(
            customer_id=customer_id,
            request_no=request_no,
            invoice_data=payload.model_dump(),
        )
        return CustomerServiceRequestRead.model_validate(row)
    except (
        CustomerOperationNotFound,
        CustomerOperationConflict,
        CustomerOperationValidationError,
        CustomerOperationDependencyError,
    ) as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")
