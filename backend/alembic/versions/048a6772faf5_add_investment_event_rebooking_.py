"""add investment event rebooking idempotency index

Revision ID: 048a6772faf5
Revises: c68cb5de2aea
Create Date: 2026-09-23 04:48:46.377512

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '048a6772faf5'
down_revision: Union[str, None] = 'c68cb5de2aea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SECTION 2 (final freeze patch): database-level backstop for the
    # rebooking idempotency fix - at most one REBOOKED InvestmentEvent
    # per (investment_id, reference) where reference carries the
    # client-supplied idempotency_key. This protects a rate-only or
    # maturity-only rebooking (no InvestmentTransaction ever created for
    # those) exactly as it protects a principal top-up.
    op.execute(
        "CREATE UNIQUE INDEX ux_investment_events_rebooking_idempotency_key "
        "ON investment_events (investment_id, reference) "
        "WHERE event_type = 'REBOOKED' AND reference IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_investment_events_rebooking_idempotency_key")
