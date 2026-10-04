"""currency decimal places limited to persisted monetary precision

Revision ID: 4792dd5b9d52
Revises: c08d1cbdad93
Create Date: 2026-10-04 11:06:34.587349

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4792dd5b9d52'
down_revision: Union[str, None] = 'c08d1cbdad93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Treasury OS persists monetary amounts at 2 decimal places system-wide
    # (every monetary column is Numeric(20,2); see app/core/monetary.py), so a
    # currency must not declare more precision than the ledger can hold -
    # PostgreSQL would silently round such amounts on insert. This migration
    # ADDS A GUARD ONLY: no column is widened and no monetary value is
    # rewritten. Existing 2-decimal values are untouched by construction.
    #
    # Pre-flight: if any existing currency already declares an unsupported
    # precision, stop with an explicit error instead of silently altering
    # master data - an operator must decide how to handle that currency.
    bad = op.get_bind().execute(
        sa.text("SELECT code, decimal_places FROM currencies WHERE decimal_places NOT BETWEEN 0 AND 2")
    ).fetchall()
    if bad:
        raise RuntimeError(
            "Cannot add ck_currencies_decimal_places_supported: currencies with unsupported "
            f"decimal_places exist: {[tuple(r) for r in bad]}. Persisted monetary precision is 2."
        )
    op.create_check_constraint(
        "ck_currencies_decimal_places_supported", "currencies", "decimal_places BETWEEN 0 AND 2",
    )


def downgrade() -> None:
    # Dropping a CHECK constraint cannot lose or alter any data (it only
    # removes a restriction), so this downgrade is safe and non-destructive.
    op.drop_constraint("ck_currencies_decimal_places_supported", "currencies", type_="check")
