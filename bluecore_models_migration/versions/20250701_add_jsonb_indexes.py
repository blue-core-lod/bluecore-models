"""Add JSONB indexes for works, instances, other_resources

Revision ID: 20250701
Revises: 3be9942c0ebe
Create Date: 2025-07-01

"""

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20250701"
down_revision: str = "3be9942c0ebe"
branch_labels = None
depends_on = None


def upgrade():
    # ==============================================================================
    # WORKS
    # ==============================================================================

    op.execute(
        "CREATE INDEX IF NOT EXISTS index_works_on_data_id ON works ((data ->> '@id'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS index_works_on_data_type ON works ((data ->> '@type'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS index_works_on_data_language ON works ((data -> 'language' ->> '@id'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS index_works_on_data_gin ON works USING gin (data)"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_works_on_data_contribution_agent
        ON works
        USING gin (jsonb_path_query_array(data, '$.contribution[*].agent'))
        """
    )

    # ==============================================================================
    # INSTANCES
    # ==============================================================================

    op.execute(
        "CREATE INDEX IF NOT EXISTS index_instances_on_data_id ON instances ((data ->> '@id'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS index_instances_on_data_type ON instances ((data ->> '@type'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS index_instances_on_data_carrier ON instances ((data -> 'carrier' ->> '@id'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS index_instances_on_data_gin ON instances USING gin (data)"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS index_instances_on_data_identified_by
        ON instances
        USING gin (jsonb_path_query_array(data, '$.identifiedBy[*]'))
        """
    )

    # ==============================================================================
    # OTHER_RESOURCES
    # ==============================================================================

    op.execute(
        "CREATE INDEX IF NOT EXISTS index_other_resources_on_data_id ON other_resources ((data ->> '@id'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS index_other_resources_on_data_type ON other_resources ((data ->> '@type'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS index_other_resources_on_data_gin ON other_resources USING gin (data)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS index_works_on_data_id")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_type")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_language")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_gin")
    op.execute("DROP INDEX IF EXISTS index_works_on_data_contribution_agent")

    op.execute("DROP INDEX IF EXISTS index_instances_on_data_id")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_type")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_carrier")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_gin")
    op.execute("DROP INDEX IF EXISTS index_instances_on_data_identified_by")

    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_id")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_type")
    op.execute("DROP INDEX IF EXISTS index_other_resources_on_data_gin")
