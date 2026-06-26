"""Add templates table and migrate is_profile OtherResources into it

Templates (formerly OtherResources with is_profile=True) become their own
polymorphic ResourceBase subclass. Existing profile rows are re-tagged as
'templates', moved out of other_resources, and given a uuid + a minted
``.../templates/{uuid}`` uri so they are addressed like Works/Instances/Hubs.
Finally the now-unused other_resources.is_profile column is dropped.

Revision ID: 20260626
Revises: 20260625
Create Date: 2026-06-26
"""

import os

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260626"
down_revision: str = "20260625"
branch_labels = None
depends_on = None


def _base_url() -> str:
    return os.environ.get("BLUECORE_URL", "https://bcld.info/").rstrip("/")


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["id"], ["resource_base.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # Move the is_profile OtherResources into the templates table.
    op.execute(
        """
        INSERT INTO templates (id)
        SELECT id FROM other_resources WHERE is_profile = true
        """
    )
    op.execute(
        """
        UPDATE resource_base SET type = 'templates'
        WHERE id IN (SELECT id FROM other_resources WHERE is_profile = true)
        """
    )
    op.execute(
        "DELETE FROM other_resources WHERE is_profile = true"
    )

    # Give every template a uuid and a minted bcld uri so they are uniform with
    # Works/Instances/Hubs (replacing any external Sinopia profile uri).
    op.execute(
        "UPDATE resource_base SET uuid = gen_random_uuid() "
        "WHERE type = 'templates' AND uuid IS NULL"
    )
    op.execute(
        f"UPDATE resource_base SET uri = '{_base_url()}/templates/' || uuid::text "
        "WHERE type = 'templates'"
    )

    op.drop_column("other_resources", "is_profile")


def downgrade() -> None:
    op.add_column(
        "other_resources",
        sa.Column(
            "is_profile",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # Return templates to other_resources as is_profile rows.
    op.execute(
        """
        INSERT INTO other_resources (id, is_profile)
        SELECT id, true FROM templates
        """
    )
    op.execute(
        """
        UPDATE resource_base SET type = 'other_resources'
        WHERE id IN (SELECT id FROM templates)
        """
    )
    op.drop_table("templates")

    # The original (Sinopia) uris overwritten in upgrade() cannot be recovered;
    # downgraded rows keep their minted .../templates/{uuid} uri.

    op.alter_column("other_resources", "is_profile", server_default=None)
