"""Prevent duplicate active cases for the same order item."""

from alembic import op
import sqlalchemy as sa


revision = "20260926_0012"
down_revision = "20260926_0011"
branch_labels = None
depends_on = None


_ACTIVE_STATUS_SQL = """
status IN (
    'DRAFT',
    'PENDING_CONFIRMATION',
    'WAITING_EVIDENCE',
    'SUBMITTED',
    'UNDER_REVIEW',
    'APPROVED',
    'EXECUTING',
    'CANCEL_PENDING',
    'REFUND_PENDING',
    'EXECUTION_FAILED'
)
"""


def upgrade() -> None:
    op.create_index(
        "uq_after_sales_cases_active_order_item",
        "after_sales_cases",
        ["customer_id", "order_no", "order_item_no"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_STATUS_SQL),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_after_sales_cases_active_order_item",
        table_name="after_sales_cases",
    )
