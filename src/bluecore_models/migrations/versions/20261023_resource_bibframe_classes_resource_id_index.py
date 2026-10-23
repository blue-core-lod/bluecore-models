"""index resource_bibframe_classes.resource_id

Revision ID: 20261023
Revises: 20261004
Create Date: 2026-10-23

update_bf_classes looks up a resource's Bibframe classes by resource_id each
time a resource is updated, and deleting from resource_base has to find the
rows that reference it. Without an index on resource_id, both scan the whole
table, and the table gains rows with every resource saved.

The index is built CONCURRENTLY so that loads can keep writing to the table
while it is created.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261023"
down_revision: str | None = "20261004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "index_resource_bibframe_classes_on_resource_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX_NAME,
            "resource_bibframe_classes",
            ["resource_id"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="resource_bibframe_classes",
            postgresql_concurrently=True,
            if_exists=True,
        )
