"""Add is_nested to profiles

A template is "nested" when another template points at it through a property
typed ``sinopia:propertyType/resource``, whose ``hasResourceAttributes`` node
names the target with ``sinopia:hasResourceTemplateId``. The flag lets nested
templates be hidden from template search so catalogers only pick top-level ones.

Existing rows are backfilled from the profiles already stored, so templates
nested before this migration drop out of template search on deploy rather than
waiting to be re-saved. The flag is maintained from then on by the
insert/update listeners in ``models/profile.py``.

Revision ID: 20260831
Revises: 20260826
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20260831"
down_revision: str | None = "20260826"
branch_labels = None
depends_on = None


SINOPIA = "http://sinopia.io/vocabulary/"

# Mirrors sync_nested_flags in models/profile.py: a template is nested when some
# *other* template names it with hasResourceTemplateId, matched against the
# target's own hasResourceId. Nesting is many-to-one, so any single referrer is
# enough and EXISTS stops at the first.
BACKFILL = f"""
UPDATE profiles p SET is_nested = true
WHERE EXISTS (
    SELECT 1
    FROM resource_base child
    CROSS JOIN LATERAL jsonb_path_query(
        child.data,
        '$[*]."{SINOPIA}hasResourceId"[*].keyvalue() ? (@.key == "@value" || @.key == "@id").value'
    ) AS own_id
    JOIN resource_base parent
      ON parent.type = 'profiles'
     AND parent.id <> child.id
     AND EXISTS (
           SELECT 1
           FROM jsonb_path_query(
             parent.data,
             '$[*]."{SINOPIA}hasResourceTemplateId"[*].keyvalue() ? (@.key == "@value" || @.key == "@id").value'
           ) AS ref
           WHERE ref = own_id
         )
    WHERE child.id = p.id
      AND child.type = 'profiles'
)
"""


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column(
            "is_nested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(BACKFILL)


def downgrade() -> None:
    op.drop_column("profiles", "is_nested")
