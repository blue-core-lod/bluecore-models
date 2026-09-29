"""merge multiple heads

Revision ID: 29ff8e09c98a
Revises: 20260909, 20260916
Create Date: 2026-09-29 12:08:33.867908

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "29ff8e09c98a"
down_revision: tuple[str, str] = ("20260909", "20260916")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
