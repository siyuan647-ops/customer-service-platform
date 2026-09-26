"""Add multimodal evidence precheck fields."""

from alembic import op
import sqlalchemy as sa


revision = "20260926_0014"
down_revision = "20260926_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "after_sales_evidence",
        sa.Column(
            "analysis_status",
            sa.String(24),
            nullable=False,
            server_default="NOT_REQUESTED",
        ),
    )
    op.add_column(
        "after_sales_evidence",
        sa.Column(
            "analysis_result",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "after_sales_evidence",
        sa.Column("analysis_model", sa.String(128)),
    )
    op.add_column(
        "after_sales_evidence",
        sa.Column("analysis_prompt_version", sa.String(64)),
    )
    op.add_column(
        "after_sales_evidence",
        sa.Column("analysis_error", sa.Text()),
    )
    op.add_column(
        "after_sales_evidence",
        sa.Column("analyzed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("after_sales_evidence", "analyzed_at")
    op.drop_column("after_sales_evidence", "analysis_error")
    op.drop_column("after_sales_evidence", "analysis_prompt_version")
    op.drop_column("after_sales_evidence", "analysis_model")
    op.drop_column("after_sales_evidence", "analysis_result")
    op.drop_column("after_sales_evidence", "analysis_status")
