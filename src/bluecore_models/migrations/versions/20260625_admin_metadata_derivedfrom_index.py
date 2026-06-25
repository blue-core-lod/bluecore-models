"""Re-point the derivedFrom btree index at the nested adminMetadata derivedFrom

bf:derivedFrom is now recorded inside a resource's nested adminMetadata rather
than as a top-level assertion, so the old btree index on
(data -> 'derivedFrom' ->> '@id') no longer matches anything. adminMetadata is
an array and derivedFrom can sit on any element, so a fixed path won't do;
instead we index jsonb_path_query_first(...) which is IMMUTABLE and returns the
single derivedFrom @id regardless of position. Dedup lookups query that same
expression for equality.

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
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_derivedFrom_id
        ON resource_base (
            (jsonb_path_query_first(data, '$.adminMetadata[*].derivedFrom."@id"') #>> '{}')
        )
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS index_resource_base_on_data_derivedFrom_id")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_resource_base_on_data_derivedFrom_id
        ON resource_base ((data -> 'derivedFrom' ->> '@id'))
        """
    )
