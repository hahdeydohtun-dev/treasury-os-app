"""stage5c matching engine enum values and suggestion idempotency indexes

Revision ID: 3f41c4e2dec4
Revises: baa2eceadc5a
Create Date: 2026-09-25 10:28:48.707569

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3f41c4e2dec4'
down_revision: Union[str, None] = 'baa2eceadc5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SECTION 21 (Stage 5C): extend the existing matchtype enum with the
    # exact-vs-tolerance distinction Stage 5C's scoring model requires -
    # the same additive ALTER TYPE ADD VALUE pattern already used
    # throughout this codebase (Stage 4's TreasuryAction/
    # ForecastSourceType extensions).
    op.execute("ALTER TYPE matchtype ADD VALUE IF NOT EXISTS 'EXACT'")
    op.execute("ALTER TYPE matchtype ADD VALUE IF NOT EXISTS 'TOLERANCE'")

    # SECTION 27: idempotency backstop, database-level, behind the
    # application-level check in reconciliation_matching_engine.py. Two
    # partial unique indexes because NULL treasury_transaction_id
    # ("no candidate found") needs its own uniqueness rule distinct from
    # the real-candidate case - a plain composite UNIQUE constraint would
    # treat every NULL as distinct and not protect the no-candidate case
    # at all.
    op.execute(
        "CREATE UNIQUE INDEX ux_reconciliation_match_suggestions_identity "
        "ON reconciliation_match_suggestions "
        "(reconciliation_run_id, bank_statement_transaction_id, treasury_transaction_id) "
        "WHERE treasury_transaction_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_reconciliation_match_suggestions_no_candidate "
        "ON reconciliation_match_suggestions "
        "(reconciliation_run_id, bank_statement_transaction_id) "
        "WHERE treasury_transaction_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_reconciliation_match_suggestions_no_candidate")
    op.execute("DROP INDEX IF EXISTS ux_reconciliation_match_suggestions_identity")
    # Postgres does not support removing enum values; the EXACT/TOLERANCE
    # values are left in place on downgrade (same convention as every
    # other enum-extension migration in this codebase).
