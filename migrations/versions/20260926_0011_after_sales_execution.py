"""Add after-sales review, execution, and outbox persistence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260926_0011"
down_revision = "20260926_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("after_sales_cases", sa.Column("resolution_action", sa.String(32)))
    op.add_column("after_sales_cases", sa.Column("refund_amount", sa.Numeric(12, 2)))
    op.add_column("after_sales_cases", sa.Column("failure_reason", sa.Text()))
    op.add_column("after_sales_cases", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.add_column("after_sales_cases", sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.add_column("after_sales_cases", sa.Column("rejected_at", sa.DateTime(timezone=True)))
    op.add_column(
        "after_sales_cases", sa.Column("execution_started_at", sa.DateTime(timezone=True))
    )
    op.add_column("after_sales_cases", sa.Column("completed_at", sa.DateTime(timezone=True)))

    op.create_table(
        "after_sales_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_id", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("action", sa.String(32)),
        sa.Column("refund_amount", sa.Numeric(12, 2)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["case_id"], ["after_sales_cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_after_sales_reviews_case_id", "after_sales_reviews", ["case_id"])

    op.create_table(
        "after_sales_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_type", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("external_request_id", sa.String(160)),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["case_id"], ["after_sales_cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "operation_type", "idempotency_key",
            name="uq_after_sales_operation_idempotency",
        ),
    )
    op.create_index("ix_after_sales_operations_case_id", "after_sales_operations", ["case_id"])
    op.create_index(
        "ix_after_sales_operations_case_created",
        "after_sales_operations",
        ["case_id", "created_at"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_events_idempotency"),
    )
    op.create_index(
        "ix_outbox_events_pending", "outbox_events", ["status", "available_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_after_sales_operations_case_created", table_name="after_sales_operations")
    op.drop_index("ix_after_sales_operations_case_id", table_name="after_sales_operations")
    op.drop_table("after_sales_operations")
    op.drop_index("ix_after_sales_reviews_case_id", table_name="after_sales_reviews")
    op.drop_table("after_sales_reviews")
    op.drop_column("after_sales_cases", "completed_at")
    op.drop_column("after_sales_cases", "execution_started_at")
    op.drop_column("after_sales_cases", "rejected_at")
    op.drop_column("after_sales_cases", "approved_at")
    op.drop_column("after_sales_cases", "reviewed_at")
    op.drop_column("after_sales_cases", "failure_reason")
    op.drop_column("after_sales_cases", "refund_amount")
    op.drop_column("after_sales_cases", "resolution_action")
