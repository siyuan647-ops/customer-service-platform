"""Add customer-provided after-sales problem type."""

from alembic import op
import sqlalchemy as sa


revision = "20260926_0010"
down_revision = "20260926_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "after_sales_cases",
        sa.Column("problem_type", sa.String(length=100), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE after_sales_cases "
            "SET problem_discovered_at_required = false "
            "WHERE problem_discovered_at_required = true"
        )
    )


def downgrade() -> None:
    op.drop_column("after_sales_cases", "problem_type")
