"""Swap the derivedFrom btree index for a JSONB containment GIN index

bf:derivedFrom is now recorded inside a resource's nested adminMetadata rather
than as a top-level assertion, so the old btree index on
(data -> 'derivedFrom' ->> '@id') is no longer used. Dedup lookups now use a
JSONB containment query (data @> ...), which is served by a GIN index using
jsonb_path_ops.

Revision ID: 20260625
Revises: a2d0be58df6c
Create Date: 2026-06-25
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260625"
down_revision: str = "a2d0be58df6c"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_derivedFrom_id")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_gin
        ON resource_base USING gin (data jsonb_path_ops)
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_gin")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_derivedFrom_id
        ON resource_base ((data -> 'derivedFrom' ->> '@id'))
        """
    )
