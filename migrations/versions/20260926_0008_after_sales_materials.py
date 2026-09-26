"""Add structured after-sales problem details."""

from alembic import op
import sqlalchemy as sa


revision = "20260926_0008"
down_revision = "20260926_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "after_sales_cases",
        sa.Column("problem_description", sa.Text(), nullable=True),
    )
    op.add_column(
        "after_sales_cases",
        sa.Column("problem_discovered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "after_sales_cases",
        sa.Column(
            "problem_discovered_at_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("after_sales_cases", "problem_discovered_at_required")
    op.drop_column("after_sales_cases", "problem_discovered_at")
    op.drop_column("after_sales_cases", "problem_description")
