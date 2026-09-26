"""Persist deterministic after-sales evidence deadlines."""

from alembic import op
import sqlalchemy as sa


revision = "20260926_0009"
down_revision = "20260926_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "after_sales_cases",
        sa.Column("evidence_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "after_sales_cases",
        sa.Column("deadline_window_hours", sa.Integer(), nullable=True),
    )
    op.add_column(
        "after_sales_cases",
        sa.Column("deadline_status", sa.String(length=24), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("after_sales_cases", "deadline_status")
    op.drop_column("after_sales_cases", "deadline_window_hours")
    op.drop_column("after_sales_cases", "evidence_deadline_at")
