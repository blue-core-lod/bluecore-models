"""Add JSONB indexes for resource_base

Revision ID: 20250728
Revises: 3be9942c0ebe
Create Date: 2025-07-28
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20250701"
down_revision: str = "3be9942c0ebe"
branch_labels = None
depends_on = None


def upgrade():
    ####################################
    ##  Create the pg_trgm extension  ##
    ####################################
    # For trigram indexing for iLike and pattern matching
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    ######################------------------------------------------------------
    ##  Resource Base  ##
    #####################

    # GIN TRGM: Pattern match (LIKE/ILIKE) on mainTitle inside title
    # Use: WHERE data -> 'title' ->> 'mainTitle' ILIKE '%Cat%'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_mainTitle_trgm
        ON resource_base
        USING gin ((data -> 'title' ->> 'mainTitle') gin_trgm_ops)
        """
    )

    # BTREE: Fast exact match on RDF @id
    # Use: WHERE data ->> '@id' = 'https://bcld.info/works/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_id
        ON resource_base ((data ->> '@id'))
        """
    )

    # BTREE: Exact match on derivedFrom @id field
    # Use: WHERE data -> 'derivedFrom' ->> '@id' = 'http://id.loc.gov/resources/instances/abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_derivedFrom_id
        ON resource_base ((data -> 'derivedFrom' ->> '@id'))
        """
    )

    # BTREE: Exact match on native UUID
    # Use: WHERE uuid = 'abc123'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_uuid
        ON resource_base (uuid)
        """
    )

    # GIN: Fallback index for containment search on any key
    # Use: WHERE data @> '{"title": {"mainTitle": "Le mal joli"}}'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_gin
        ON resource_base
        USING gin (data)
        """
    )

def downgrade():
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_gin")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_uuid")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_derivedFrom_id")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_id")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_mainTitle_trgm")