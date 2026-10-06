"""Add durable customer service operation requests."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260929_0016"
down_revision = "20260929_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "after_sales_cases",
        "case_type",
        existing_type=sa.String(24),
        nullable=True,
    )
    op.add_column(
        "orders",
        sa.Column(
            "shipping_address",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.create_table(
        "customer_service_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_no", sa.String(40), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_no", sa.String(32), nullable=False),
        sa.Column("request_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
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
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_no"),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_customer_service_requests_customer_idempotency",
        ),
    )
    op.create_index(
        "ix_customer_service_requests_conversation_id",
        "customer_service_requests",
        ["conversation_id"],
    )
    op.create_index(
        "ix_customer_service_requests_customer_id",
        "customer_service_requests",
        ["customer_id"],
    )
    op.create_index(
        "ix_customer_service_requests_order_no",
        "customer_service_requests",
        ["order_no"],
    )
    op.create_index(
        "ix_customer_service_requests_customer_created",
        "customer_service_requests",
        ["customer_id", "created_at"],
    )
    op.create_index(
        "ix_customer_service_requests_order_type",
        "customer_service_requests",
        ["order_no", "request_type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_service_requests_order_type",
        table_name="customer_service_requests",
    )
    op.drop_index(
        "ix_customer_service_requests_customer_created",
        table_name="customer_service_requests",
    )
    op.drop_index(
        "ix_customer_service_requests_order_no",
        table_name="customer_service_requests",
    )
    op.drop_index(
        "ix_customer_service_requests_customer_id",
        table_name="customer_service_requests",
    )
    op.drop_index(
        "ix_customer_service_requests_conversation_id",
        table_name="customer_service_requests",
    )
    op.drop_table("customer_service_requests")
    op.drop_column("orders", "shipping_address")
    op.execute(
        "UPDATE after_sales_cases SET case_type = 'refund' WHERE case_type IS NULL"
    )
    op.alter_column(
        "after_sales_cases",
        "case_type",
        existing_type=sa.String(24),
        nullable=False,
    )
