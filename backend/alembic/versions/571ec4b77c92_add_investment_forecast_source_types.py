"""add investment forecast source types

Revision ID: 571ec4b77c92
Revises: bede4c1c3704
Create Date: 2026-09-21 11:24:26.880071

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '571ec4b77c92'
down_revision: Union[str, None] = 'bede4c1c3704'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SECTION 20: extend the existing forecastsourcetype enum with the
    # investment-driven source types (INVESTMENT_PLACEMENT was already
    # covered by FUTURE_INVESTMENT historically, but the specific
    # maturity-principal/maturity-interest/interest-receipt/termination
    # traceability this stage requires needs its own distinct values).
    op.execute("ALTER TYPE forecastsourcetype ADD VALUE IF NOT EXISTS 'INVESTMENT_PLACEMENT'")
    op.execute("ALTER TYPE forecastsourcetype ADD VALUE IF NOT EXISTS 'INVESTMENT_MATURITY_PRINCIPAL'")
    op.execute("ALTER TYPE forecastsourcetype ADD VALUE IF NOT EXISTS 'INVESTMENT_MATURITY_INTEREST'")
    op.execute("ALTER TYPE forecastsourcetype ADD VALUE IF NOT EXISTS 'INVESTMENT_INTEREST_RECEIPT'")
    op.execute("ALTER TYPE forecastsourcetype ADD VALUE IF NOT EXISTS 'INVESTMENT_TERMINATION'")


def downgrade() -> None:
    # Postgres does not support removing enum values; downgrade is a no-op.
    pass
