"""add place terminate rollover rebook treasury actions

Revision ID: bede4c1c3704
Revises: c6a51871be60
Create Date: 2026-09-21 09:45:50.668840

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bede4c1c3704'
down_revision: Union[str, None] = 'c6a51871be60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SECTION 28: extend the existing TreasuryAction enum with the new
    # action verbs Stage 4 needs (PLACE, TERMINATE, ROLLOVER, REBOOK) -
    # reusing the same permission framework, never a parallel one.
    op.execute("ALTER TYPE treasuryaction ADD VALUE IF NOT EXISTS 'PLACE'")
    op.execute("ALTER TYPE treasuryaction ADD VALUE IF NOT EXISTS 'TERMINATE'")
    op.execute("ALTER TYPE treasuryaction ADD VALUE IF NOT EXISTS 'ROLLOVER'")
    op.execute("ALTER TYPE treasuryaction ADD VALUE IF NOT EXISTS 'REBOOK'")


def downgrade() -> None:
    # Postgres does not support removing enum values; downgrade is a no-op.
    pass
