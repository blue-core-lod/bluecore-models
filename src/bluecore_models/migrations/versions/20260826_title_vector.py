"""add a title-only search vector

Revision ID: 20260826
Revises: 20260817
Create Date: 2026-08-26

The stored vector covers main titles (including VariantTitle and ParallelTitle
objects) and subtitles without including unrelated resource values.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826"
down_revision: str | None = "20260817"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the generated title vector and its GIN lookup index."""
    op.add_column(
        "resource_base",
        sa.Column(
            "title_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(jsonb_to_tsv('simple', data->'title', 'mainTitle'), 'A') || "
                "setweight(jsonb_to_tsv('english', data->'title', 'mainTitle'), 'A') || "
                "setweight(jsonb_to_tsv('simple', data->'title', 'subtitle'), 'B') || "
                "setweight(jsonb_to_tsv('english', data->'title', 'subtitle'), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("index_resource_base_on_title_vector"),
        "resource_base",
        ["title_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Remove the title index before dropping its generated column."""
    op.drop_index(
        op.f("index_resource_base_on_title_vector"),
        table_name="resource_base",
    )
    op.drop_column("resource_base", "title_vector")
