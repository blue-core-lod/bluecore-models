"""add instance_exports table

Revision ID: 20261005
Revises: 20261004
Create Date: 2026-10-05

Records who exported an Instance (e.g. to FOLIO or Alma), on behalf of which
Keycloak group, when, and the identifier it received there. Both foreign keys
are ON DELETE SET NULL so the rows outlive the Instance and Version they
describe.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261005"
down_revision: str | None = "20261004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instance_exports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=True),
        sa.Column("instance_uri", sa.String(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("keycloak_user_id", sa.String(length=128), nullable=False),
        sa.Column("keycloak_username", sa.String(length=255), nullable=True),
        sa.Column("keycloak_group", sa.String(length=255), nullable=False),
        sa.Column("target_identifier", sa.String(length=255), nullable=True),
        sa.Column("exported_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["version_id"], ["versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "index_instance_exports_on_instance_id_exported_at",
        "instance_exports",
        ["instance_id", "exported_at"],
    )
    op.create_index(
        "index_instance_exports_on_instance_uri",
        "instance_exports",
        ["instance_uri"],
    )
    op.create_index(
        "index_instance_exports_on_keycloak_group_exported_at",
        "instance_exports",
        ["keycloak_group", "exported_at"],
    )
    op.create_index(
        "index_instance_exports_on_keycloak_user_id",
        "instance_exports",
        ["keycloak_user_id"],
    )
    op.create_index(
        "index_instance_exports_on_target_identifier",
        "instance_exports",
        ["target_identifier"],
    )


# Dropping the table drops its indexes and foreign keys with it
def downgrade() -> None:
    op.drop_table("instance_exports")
