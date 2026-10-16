"""add an indexed, normalized identifiers column

Revision ID: 20261004
Revises: 20261001
Create Date: 2026-10-04

Adds resource_base.identifiers: the ISBNs, ISSNs, LCCNs and DOIs in a record's
top-level identifiedBy, cleaned up so lookups can match exactly. Other kinds of
identifiers are left out.
See bluecore_identifiers in pg_ext_func.py for the rules.

Postgres keeps the column up to date on every insert and update, so new records
are covered automatically, and adding the column fills it in for every existing
record.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from bluecore_models.models.pg_ext_func import (
    BLUECORE_IDENTIFIER_VALUES,
    BLUECORE_IDENTIFIERS,
    BLUECORE_ISBN10_CHECK_DIGIT,
    BLUECORE_ISBN13_CHECK_DIGIT,
)

revision: str = "20261004"
down_revision: str | None = "20261001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Creates the cleanup functions, then the identifiers column and its index
def upgrade() -> None:
    op.execute(BLUECORE_ISBN10_CHECK_DIGIT)
    op.execute(BLUECORE_ISBN13_CHECK_DIGIT)
    op.execute(BLUECORE_IDENTIFIER_VALUES)
    op.execute(BLUECORE_IDENTIFIERS)
    op.add_column(
        "resource_base",
        sa.Column(
            "identifiers",
            postgresql.ARRAY(sa.Text()),
            sa.Computed("bluecore_identifiers(data)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("index_resource_base_on_identifiers"),
        "resource_base",
        ["identifiers"],
        unique=False,
        postgresql_using="gin",
    )
    # Tells Postgres that each identifier is rare, so lookups use the index
    op.execute("ALTER TABLE resource_base ALTER COLUMN identifiers SET STATISTICS 1000")
    op.execute("ANALYZE resource_base (identifiers)")


# Removes the index and column first, since they depend on the functions
def downgrade() -> None:
    op.drop_index(
        op.f("index_resource_base_on_identifiers"),
        table_name="resource_base",
    )
    op.drop_column("resource_base", "identifiers")
    op.execute("DROP FUNCTION IF EXISTS public.bluecore_identifiers(jsonb)")
    op.execute("DROP FUNCTION IF EXISTS public.bluecore_identifier_values(text, text)")
    op.execute("DROP FUNCTION IF EXISTS public.bluecore_isbn13_check_digit(text)")
    op.execute("DROP FUNCTION IF EXISTS public.bluecore_isbn10_check_digit(text)")
