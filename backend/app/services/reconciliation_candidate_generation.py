"""
Database-side candidate generation for Stage 5C matching (SECTIONS 6-9,
16-18, 36-38).

This module is the ONLY place that queries for candidate
TreasuryTransaction rows - it exists specifically so scoring
(reconciliation_scoring.py) never has to load an unbounded population
and compare in Python. Every filter here is an indexed SQL predicate:
entity, bank account, currency, direction compatibility, a narrow date
window (the run's own configured tolerance, never the full run period,
per SECTION 17), an amount-tolerance band, POSTED status only, and
excluding TreasuryTransaction rows already claimed by an existing
PENDING/ACCEPTED suggestion from ANY run (SECTION 39 - a transaction
that already has a live suggestion is not offered as a fresh candidate
again).
"""
import datetime
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import BankStatementEntryType
from app.models.lookup import CashDirection
from app.models.reconciliation import MatchSuggestionStatus, ReconciliationMatchSuggestion
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction

_COMPATIBLE_DIRECTION = {
    BankStatementEntryType.CREDIT: CashDirection.INFLOW,
    BankStatementEntryType.DEBIT: CashDirection.OUTFLOW,
}


async def already_claimed_treasury_transaction_ids(db: AsyncSession, legal_entity_id: uuid.UUID) -> set:
    """
    SECTION 39: TreasuryTransaction rows already referenced by a live
    (PENDING or ACCEPTED) suggestion from ANY run are excluded from
    candidate generation, so the engine never proposes a fresh primary
    match for something that already has one. REJECTED/SUPERSEDED
    suggestions do NOT hold a claim - a rejected suggestion's
    transaction remains eligible for a future run's candidate pool.
    """
    stmt = select(ReconciliationMatchSuggestion.treasury_transaction_id).where(
        ReconciliationMatchSuggestion.status.in_(
            (MatchSuggestionStatus.PENDING, MatchSuggestionStatus.ACCEPTED)
        ),
        ReconciliationMatchSuggestion.treasury_transaction_id.is_not(None),
    )
    result = await db.execute(stmt)
    return {row[0] for row in result.all()}


async def generate_candidates(
    db: AsyncSession, *, legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID, currency_code: str,
    bank_entry_type: BankStatementEntryType, bank_transaction_date: datetime.date, bank_amount: Decimal,
    date_tolerance_days: int, amount_tolerance_pct: Decimal, excluded_treasury_transaction_ids: set,
) -> list:
    """
    SECTION 7/8/9: hard entity/bank-account/currency isolation - these
    three predicates are never relaxed for any reason, and appear in
    every query this function ever issues. SECTION 15: only
    TreasuryTransaction rows whose direction is compatible with the bank
    entry type are even fetched - an incompatible direction is excluded
    at the SQL level, not merely scored zero. SECTION 38: only `POSTED`
    TreasuryTransaction rows are eligible - `PENDING` (not yet real) and
    `CANCELLED` (reversed) rows are never offered as candidates.
    """
    compatible_direction = _COMPATIBLE_DIRECTION[bank_entry_type]

    window_start = bank_transaction_date - datetime.timedelta(days=date_tolerance_days)
    window_end = bank_transaction_date + datetime.timedelta(days=date_tolerance_days)

    allowed_amount_diff = bank_amount * (amount_tolerance_pct / Decimal(100))
    amount_min = bank_amount - allowed_amount_diff
    amount_max = bank_amount + allowed_amount_diff

    stmt = select(TreasuryTransaction).where(
        TreasuryTransaction.legal_entity_id == legal_entity_id,
        TreasuryTransaction.bank_account_id == bank_account_id,
        TreasuryTransaction.transaction_currency_code == currency_code,
        TreasuryTransaction.direction == compatible_direction,
        TreasuryTransaction.status == TransactionStatus.POSTED,
        TreasuryTransaction.event_date >= window_start,
        TreasuryTransaction.event_date <= window_end,
        TreasuryTransaction.transaction_amount >= amount_min,
        TreasuryTransaction.transaction_amount <= amount_max,
    )
    if excluded_treasury_transaction_ids:
        stmt = stmt.where(TreasuryTransaction.id.not_in(excluded_treasury_transaction_ids))

    result = await db.execute(stmt)
    return list(result.scalars().all())
