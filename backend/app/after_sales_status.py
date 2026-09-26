from __future__ import annotations


# A case remains active until the customer request has reached a business
# terminal state. Keep this tuple as the single source used by application
# queries and the database partial unique index.
ACTIVE_AFTER_SALES_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "WAITING_MATERIALS",
    "SUBMITTED",
    "UNDER_REVIEW",
    "APPROVED",
    "EXECUTING",
    "CANCEL_PENDING",
    "REFUND_PENDING",
    "EXECUTION_FAILED",
)


ACTIVE_AFTER_SALES_STATUS_SQL = (
    "status IN ("
    + ", ".join(f"'{status}'" for status in ACTIVE_AFTER_SALES_STATUSES)
    + ")"
)
