"""Add JSONB indexes for resource_base

Revision ID: 20250728
Revises: 90f448095118
Create Date: 2025-07-28
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20250728"
down_revision: str = "90f448095118"
branch_labels = None
depends_on = None


def upgrade():
    # ==========================================================================
    # Ensure pg_trgm extension is enabled for trigram indexing
    # --------------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ==========================================================================
    # Indexes for resource_base table
    # --------------------------------------------------------------------------
    # GIN: Fallback index for general containment queries on full JSONB column
    # e.g. WHERE data @> '{"title": {"mainTitle": "Le mal joli"}}'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_gin
        ON resource_base
        USING gin (data)
        """
    )

    # GIN + Trigram: Pattern match for mainTitle ILIKE search
    # e.g. WHERE data -> 'title' ->> 'mainTitle' ILIKE '%Cat%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_mainTitle_trgm
        ON resource_base
        USING gin ((data -> 'title' ->> 'mainTitle') gin_trgm_ops)
        """
    )

    # BTREE: Exact match on RDF @id
    # e.g. WHERE data ->> '@id' = 'https://bcld.info/works/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_id
        ON resource_base ((data ->> '@id'))
        """
    )

    # BTREE: Exact match on derivedFrom → @id
    # e.g. WHERE data -> 'derivedFrom' ->> '@id' = 'http://id.loc.gov/...'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_derivedFrom_id
        ON resource_base ((data -> 'derivedFrom' ->> '@id'))
        """
    )

    # BTREE: Fast match on native UUID field
    # e.g. WHERE uuid = 'abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_uuid
        ON resource_base (uuid)
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_gin")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_mainTitle_trgm")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_id")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_derivedFrom_id")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_uuid")
