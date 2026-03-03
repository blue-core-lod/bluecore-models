"""Add Keycloak user id to versions

Revision ID: da65046e4024
Revises: ad784bf46c6f
Create Date: 2025-09-29 07:43:07.109100
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "da65046e4024"
down_revision: Union[str, None] = "ad784bf46c6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add keycloak_user_id column
    op.add_column(
        "versions",
        sa.Column("keycloak_user_id", sa.String(length=128), nullable=True),
    )
    # Create a non-unique index to support lookups by user id
    op.create_index(
        "ix_versions_keycloak_user_id",
        "versions",
        ["keycloak_user_id"],
        unique=False,
    )


def downgrade() -> None:
    # Drop the index then the column
    op.drop_index("ix_versions_keycloak_user_id", table_name="versions")
    op.drop_column("versions", "keycloak_user_id")
