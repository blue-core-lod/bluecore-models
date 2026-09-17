"""versions: username column, dedup, and a unique (resource_id, created_at)

Revision ID: 20260915
Revises: 20260826
Create Date: 2026-09-15

Supports the version-history endpoints in bluecore_api, which list a resource's
versions and fetch one of them by id or by its ISO created_at value.

1. keycloak_username, so the editor can show a name rather than the opaque JWT
   'sub' GUID stored in keycloak_user_id. Nullable: every existing row, and
   every non-HTTP writer (batch ingest, migrations), leaves it NULL.

2. Deletes rows sharing (resource_id, created_at), keeping the lowest id of
   each group. These were written by add_version() before its guard was
   narrowed: a save that links an Instance to a Work fired after_update with
   only instances.work_id dirty, so resource_base was never written, updated_at
   never advanced, and the extra row duplicated both the timestamp and the data
   of the row before it. Verified on the dev and stage databases: every group
   held byte-identical data (0 groups with differing data across ~327k groups),
   so no distinct history is lost. Groups are not always pairs -- stage had 209
   groups larger than two, hence the general "keep the lowest id" form rather
   than a pairwise delete.

3. A unique constraint on (resource_id, created_at), which makes the timestamp
   an unambiguous identifier. Postgres backs it with a unique btree index on
   those columns, which also serves the per-resource listing query, so
   index_versions_on_resource_id is dropped as redundant.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915"
down_revision: str | None = "20260826"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Archive the rows before deleting them. The delete is not reversible by
# downgrade(), so keep a copy in the same transaction rather than relying on
# whoever runs the migration having taken a backup first.
ARCHIVE_DUPLICATES = """
CREATE TABLE IF NOT EXISTS versions_deleted_duplicates AS
SELECT * FROM versions v
WHERE v.id > (
    SELECT min(o.id) FROM versions o
    WHERE o.resource_id = v.resource_id AND o.created_at = v.created_at
)
"""

DELETE_DUPLICATES = """
DELETE FROM versions v
WHERE v.id > (
    SELECT min(o.id) FROM versions o
    WHERE o.resource_id = v.resource_id AND o.created_at = v.created_at
)
"""


def upgrade() -> None:
    op.add_column(
        "versions",
        sa.Column("keycloak_username", sa.String(length=255), nullable=True),
    )
    op.execute(ARCHIVE_DUPLICATES)
    op.execute(DELETE_DUPLICATES)
    # Create the constraint before dropping the old index, so resource_id is
    # never left without an index backing it.
    op.create_unique_constraint(
        "uq_versions_resource_id_created_at",
        "versions",
        ["resource_id", "created_at"],
    )
    op.drop_index(
        index_name="index_versions_on_resource_id",
        table_name="versions",
        if_exists=True,
    )


def downgrade() -> None:
    # Same ordering concern in reverse: restore the single-column index first.
    op.create_index(
        index_name="index_versions_on_resource_id",
        table_name="versions",
        columns=["resource_id"],
        unique=False,
        if_not_exists=True,
    )
    op.drop_constraint("uq_versions_resource_id_created_at", "versions", type_="unique")
    # The deleted rows are not restored. They are recoverable by hand from
    # versions_deleted_duplicates, which is left in place deliberately.
    op.drop_column("versions", "keycloak_username")
