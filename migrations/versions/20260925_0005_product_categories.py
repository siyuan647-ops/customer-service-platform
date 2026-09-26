"""Store policy product categories as an indexed array."""

from alembic import op


revision = "20260925_0005"
down_revision = "20260925_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_documents "
        "RENAME COLUMN product_category TO product_categories"
    )
    op.execute(
        r"""
        ALTER TABLE knowledge_documents
        ALTER COLUMN product_categories TYPE varchar(100)[]
        USING CASE
            WHEN product_categories LIKE '全品类%'
                THEN ARRAY['全品类']::varchar(100)[]
            ELSE regexp_split_to_array(
                product_categories,
                '\s*[、,，;；/]\s*'
            )::varchar(100)[]
        END
        """
    )
    op.execute(
        "CREATE INDEX ix_knowledge_documents_product_categories_gin "
        "ON knowledge_documents USING gin (product_categories)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_documents_product_categories_gin")
    op.execute(
        "ALTER TABLE knowledge_documents "
        "ALTER COLUMN product_categories TYPE varchar(255) "
        "USING array_to_string(product_categories, '、')"
    )
    op.execute(
        "ALTER TABLE knowledge_documents "
        "RENAME COLUMN product_categories TO product_category"
    )
