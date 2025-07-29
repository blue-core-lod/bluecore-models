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
    # Indexes for resource_base table
    # --------------------------------------------------------------------------
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
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_derivedFrom_id")
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_uuid")
