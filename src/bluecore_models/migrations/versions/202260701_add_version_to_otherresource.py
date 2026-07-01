"""Add version to OtherResource

Revision ID: c01be3b45465
Revises: 20260626
Create Date: 2026-07-01 09:32:00.707288

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260701"
down_revision: str = "58d1931e61fe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add index on versions.resource_id and add a version for each of the other_resources ###
    op.create_index(
        index_name="index_versions_on_resource_id",
        table_name="versions",
        columns=["resource_id"],
        unique=False,
        if_not_exists=True,
    )
    op.execute("""
               INSERT INTO versions (resource_id, data, keycloak_user_id, created_at)
               SELECT rb.id, rb.data, NULL, rb.updated_at
               FROM other_resources o
               JOIN resource_base rb ON rb.id = o.id
               """)
    # ### end Alembic commands ###


def downgrade() -> None:
    # Drop index on versions.resource_id and delete all versions of other resources ###
    op.drop_index(
        index_name="index_versions_on_resource_id",
        table_name="versions",
        if_exists=True,
    )
    op.execute("""
               DELETE FROM versions v USING other_resources o
               WHERE v.resource_id = o.id
               """)
    # ### end Alembic commands ###
