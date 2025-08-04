"""Create type index

Revision ID: ad784bf46c6f
Revises: 20250728
Create Date: 2025-08-01 10:30:52.579736

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "ad784bf46c6f"
down_revision: Union[str, None] = "20250728"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create index on type
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS type_idx
        ON resource_base (type)
        """
    )


def downgrade() -> None:
    # Drop index on type
    op.execute("DROP INDEX IF EXISTS type_idx")
