"""add a title-only search vector

Revision ID: 20260826
Revises: 20260817
Create Date: 2026-08-26

The stored vector covers main titles (including VariantTitle and ParallelTitle
objects) and subtitles without including unrelated resource values.

It reads titles with bluecore_titles_to_tsv instead of jsonb_to_tsv, because a
title is not always a plain string:

    "mainTitle": {"@value": "Reader's guide", "@language": "zxx-latn"}

jsonb_to_tsv would index "@value" and "zxx-latn" as if they were words in the
title. bluecore_titles_to_tsv takes just the text.

Nothing existing is rebuilt: both functions are new, and data_vector goes on
using jsonb_to_tsv.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from bluecore_models.models.pg_ext_func import (
    BLUECORE_JSONB_TEXT,
    BLUECORE_TITLES_TO_TSV,
)

revision: str = "20260826"
down_revision: str | None = "20260817"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TITLE_VECTOR = (
    "setweight(bluecore_titles_to_tsv('simple', data->'title', 'mainTitle'), 'A') || "
    "setweight(bluecore_titles_to_tsv('english', data->'title', 'mainTitle'), 'A') || "
    "setweight(bluecore_titles_to_tsv('simple', data->'title', 'subtitle'), 'B') || "
    "setweight(bluecore_titles_to_tsv('english', data->'title', 'subtitle'), 'B')"
)


def upgrade() -> None:
    """Add the title readers, then the generated column and its GIN index."""
    op.execute(BLUECORE_JSONB_TEXT)
    op.execute(BLUECORE_TITLES_TO_TSV)
    op.add_column(
        "resource_base",
        sa.Column(
            "title_vector",
            postgresql.TSVECTOR(),
            sa.Computed(TITLE_VECTOR, persisted=True),
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
    """Remove the title index and column before the functions they depend on."""
    op.drop_index(
        op.f("index_resource_base_on_title_vector"),
        table_name="resource_base",
    )
    op.drop_column("resource_base", "title_vector")
    op.execute(
        "DROP FUNCTION IF EXISTS public.bluecore_titles_to_tsv(text, jsonb, text)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.bluecore_jsonb_text(jsonb)")
