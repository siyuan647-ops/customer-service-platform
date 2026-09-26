"""Add after-sales cases, evidence, and action logs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260926_0007"
down_revision = "20260926_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "after_sales_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_no", sa.String(length=40), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("order_item_no", sa.String(length=48), nullable=False),
        sa.Column("case_type", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("should_handoff", sa.Boolean(), nullable=False),
        sa.Column("evidence_required", sa.Boolean(), nullable=False),
        sa.Column("required_information", sa.JSON(), nullable=False),
        sa.Column("recommended_actions", sa.JSON(), nullable=False),
        sa.Column("policy_references", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["human_tickets.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_no"),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_after_sales_cases_customer_idempotency",
        ),
    )
    op.create_index("ix_after_sales_cases_agent_run_id", "after_sales_cases", ["agent_run_id"])
    op.create_index("ix_after_sales_cases_conversation_id", "after_sales_cases", ["conversation_id"])
    op.create_index("ix_after_sales_cases_customer_id", "after_sales_cases", ["customer_id"])
    op.create_index(
        "ix_after_sales_cases_customer_created",
        "after_sales_cases",
        ["customer_id", "created_at"],
    )
    op.create_index("ix_after_sales_cases_order_no", "after_sales_cases", ["order_no"])
    op.create_index("ix_after_sales_cases_ticket_id", "after_sales_cases", ["ticket_id"])

    op.create_table(
        "after_sales_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["case_id"], ["after_sales_cases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_after_sales_evidence_case_id", "after_sales_evidence", ["case_id"])
    op.create_index(
        "ix_after_sales_evidence_customer_id", "after_sales_evidence", ["customer_id"]
    )

    op.create_table(
        "after_sales_action_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=24), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["case_id"], ["after_sales_cases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_after_sales_action_logs_case_id", "after_sales_action_logs", ["case_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_after_sales_action_logs_case_id", table_name="after_sales_action_logs")
    op.drop_table("after_sales_action_logs")
    op.drop_index("ix_after_sales_evidence_customer_id", table_name="after_sales_evidence")
    op.drop_index("ix_after_sales_evidence_case_id", table_name="after_sales_evidence")
    op.drop_table("after_sales_evidence")
    op.drop_index("ix_after_sales_cases_ticket_id", table_name="after_sales_cases")
    op.drop_index("ix_after_sales_cases_order_no", table_name="after_sales_cases")
    op.drop_index("ix_after_sales_cases_customer_created", table_name="after_sales_cases")
    op.drop_index("ix_after_sales_cases_customer_id", table_name="after_sales_cases")
    op.drop_index("ix_after_sales_cases_conversation_id", table_name="after_sales_cases")
    op.drop_index("ix_after_sales_cases_agent_run_id", table_name="after_sales_cases")
    op.drop_table("after_sales_cases")
