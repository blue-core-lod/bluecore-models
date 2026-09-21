"""add activity_streams_cursor table

Revision ID: 20260916
Revises: 20260915
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916"
down_revision: str | None = "20260915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "index_versions_on_resource_id_id"


def upgrade() -> None:
    activity_streams_cursor = op.create_table(
        "activity_streams_cursor",
        sa.Column("cursor_name", sa.String(length=25), nullable=False),
        sa.Column("cursor", sa.String(length=10), nullable=False),
        sa.PrimaryKeyConstraint("cursor_name"),
    )
    op.bulk_insert(
        activity_streams_cursor,
        [
            {"cursor_name": "bfdb_works", "cursor": "2026-09-10"},
            {"cursor_name": "bfdb_instances", "cursor": "2026-09-10"},
            {"cursor_name": "bfdb_hubs", "cursor": "2026-09-10"},
        ],
    )
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX_NAME,
            "versions",
            ["resource_id", "id"],
            unique=False,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="versions",
            postgresql_concurrently=True,
        )
    op.drop_table("activity_streams_cursor")
