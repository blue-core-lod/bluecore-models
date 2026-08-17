"""symbol normalization for search

Revision ID: 20260817
Revises: 911a986691ba
Create Date: 2026-08-17

Rebuilds data_vector through bluecore_normalize so bibliographic symbols survive
tokenization as sentinel tokens, ALA-LC romanization marks are deleted rather
than turned into an apostrophe by unaccent, and sub/superscripts fold to ASCII.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from bluecore_models.models.pg_ext_func import PG_EXT_FUNC

revision: str = "20260817"
down_revision: str | None = "911a986691ba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TITLE_AND_URI_TERMS = (
    "setweight(jsonb_to_tsv('simple', data->'title', 'mainTitle'), 'A') || "
    "setweight(jsonb_to_tsv('english', data->'title', 'mainTitle'), 'A') || "
    "setweight(jsonb_to_tsv('simple', data->'title', 'subtitle'), 'B') || "
    "setweight(jsonb_to_tsv('english', data->'title', 'subtitle'), 'B') || "
    "setweight(to_tsvector('simple', coalesce(uri, '')), 'C') || "
    "setweight(to_tsvector('english', coalesce(uri, '')), 'C') || "
)

NEW_DATA_VECTOR = _TITLE_AND_URI_TERMS + (
    "to_tsvector('simple', bluecore_normalize(coalesce(data::text, ''))) || "
    "to_tsvector('english', bluecore_normalize(coalesce(data::text, '')))"
)

OLD_DATA_VECTOR = _TITLE_AND_URI_TERMS + (
    "to_tsvector('simple', f_unaccent(coalesce(data::text, ''))) || "
    "to_tsvector('english', f_unaccent(coalesce(data::text, '')))"
)

# The pre-migration jsonb_to_tsv, restored on downgrade
OLD_JSONB_TO_TSV = """
CREATE OR REPLACE FUNCTION public.jsonb_to_tsv(lang_config text, data jsonb, key_name text)
RETURNS tsvector AS $$
BEGIN
  IF jsonb_typeof(data) != 'array' THEN
    RETURN to_tsvector(lang_config::regconfig, f_unaccent(coalesce(data->>key_name, '')));
  ELSE
    RETURN (
        SELECT to_tsvector(lang_config::regconfig, f_unaccent(coalesce(string_agg(value->>key_name, ' '), '')))
        FROM jsonb_array_elements(data)
    );
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE WARNING 'An unexpected error occurred for jsonb_to_tsv: %', SQLERRM;
  RETURN ''::tsvector;
END;
$$ LANGUAGE plpgsql IMMUTABLE"""


def _drop_data_vector() -> None:
    op.drop_index("index_resource_base_on_data_vector", table_name="resource_base")
    op.drop_column("resource_base", "data_vector")


def _create_data_vector(expression: str) -> None:
    op.add_column(
        "resource_base",
        sa.Column(
            "data_vector",
            postgresql.TSVECTOR(),
            sa.Computed(expression, persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("index_resource_base_on_data_vector"),
        "resource_base",
        ["data_vector"],
        unique=False,
        postgresql_using="gin",
    )


def upgrade() -> None:
    _drop_data_vector()

    for statement in PG_EXT_FUNC:
        op.execute(statement)

    _create_data_vector(NEW_DATA_VECTOR)


def downgrade() -> None:
    _drop_data_vector()
    op.execute(OLD_JSONB_TO_TSV)
    _create_data_vector(OLD_DATA_VECTOR)
    op.execute("DROP FUNCTION IF EXISTS public.bluecore_normalize(text)")
