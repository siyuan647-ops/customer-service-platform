"""Unify pre-submission after-sales states as waiting materials."""

from alembic import op
import sqlalchemy as sa


revision = "20260926_0013"
down_revision = "20260926_0012"
branch_labels = None
depends_on = None


_NEW_ACTIVE_STATUS_SQL = """
status IN (
    'DRAFT',
    'WAITING_MATERIALS',
    'SUBMITTED',
    'UNDER_REVIEW',
    'APPROVED',
    'EXECUTING',
    'CANCEL_PENDING',
    'REFUND_PENDING',
    'EXECUTION_FAILED'
)
"""

_OLD_ACTIVE_STATUS_SQL = """
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


def _create_active_case_index(predicate: str) -> None:
    op.create_index(
        "uq_after_sales_cases_active_order_item",
        "after_sales_cases",
        ["customer_id", "order_no", "order_item_no"],
        unique=True,
        postgresql_where=sa.text(predicate),
    )


def upgrade() -> None:
    op.drop_index(
        "uq_after_sales_cases_active_order_item",
        table_name="after_sales_cases",
    )
    op.execute(
        """
        UPDATE after_sales_cases
        SET status = 'WAITING_MATERIALS'
        WHERE status IN ('PENDING_CONFIRMATION', 'WAITING_EVIDENCE')
        """
    )
    op.execute(
        """
        UPDATE after_sales_action_logs
        SET from_status = 'WAITING_MATERIALS'
        WHERE from_status IN ('PENDING_CONFIRMATION', 'WAITING_EVIDENCE')
        """
    )
    op.execute(
        """
        UPDATE after_sales_action_logs
        SET to_status = 'WAITING_MATERIALS'
        WHERE to_status IN ('PENDING_CONFIRMATION', 'WAITING_EVIDENCE')
        """
    )
    _create_active_case_index(_NEW_ACTIVE_STATUS_SQL)


def downgrade() -> None:
    op.drop_index(
        "uq_after_sales_cases_active_order_item",
        table_name="after_sales_cases",
    )
    op.execute(
        """
        UPDATE after_sales_cases
        SET status = 'WAITING_EVIDENCE'
        WHERE status = 'WAITING_MATERIALS'
        """
    )
    op.execute(
        """
        UPDATE after_sales_action_logs
        SET from_status = 'WAITING_EVIDENCE'
        WHERE from_status = 'WAITING_MATERIALS'
        """
    )
    op.execute(
        """
        UPDATE after_sales_action_logs
        SET to_status = 'WAITING_EVIDENCE'
        WHERE to_status = 'WAITING_MATERIALS'
        """
    )
    _create_active_case_index(_OLD_ACTIVE_STATUS_SQL)
