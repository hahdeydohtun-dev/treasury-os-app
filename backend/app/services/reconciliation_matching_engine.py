"""
Stage 5C deterministic matching engine orchestration.

Ties together candidate generation (reconciliation_candidate_generation.py)
and scoring (reconciliation_scoring.py) into the actual per-bank-
transaction matching pass a reconciliation run's execution performs.
This module is the ONLY place that persists `ReconciliationMatchSuggestion`
rows - candidate generation and scoring remain pure/side-effect-free.

Determinism (SECTION 50): candidates are always ranked
(score DESC, event_date ASC, id ASC) before selection - a fully
explicit, stable, reproducible tie-break order, never dependent on
database row order, dict iteration, or randomness.

Financial integrity (SECTION 30): nothing in this module ever creates,
modifies, or deletes a TreasuryTransaction, BankStatementTransaction,
BankBalance, or any cash-affecting record. It only reads
TreasuryTransaction/BankStatementTransaction and writes
ReconciliationMatchSuggestion rows.
"""
import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import BankStatementTransaction
from app.models.reconciliation import (
    MatchSuggestionStatus,
    MatchType,
    ReconciliationConfiguration,
    ReconciliationMatchSuggestion,
)
from app.services.reconciliation_candidate_generation import (
    already_claimed_treasury_transaction_ids,
    generate_candidates,
)
from app.services.reconciliation_scoring import (
    AUTO_MATCH_THRESHOLD,
    MATCHING_RULE_VERSION,
    REVIEW_THRESHOLD,
    score_candidate,
)

# Conservative defaults (SECTION 10.1/24) used only when the run's own
# configuration leaves a tolerance at its default zero - matches
# ReconciliationConfiguration's own column defaults (amount_tolerance_pct
# defaults to 0, date_tolerance_days defaults to 0), so an
# unconfigured run performs EXACT-only matching, never an implicit
# tolerance the treasury team never actually configured.


@dataclass
class MatchingRunResult:
    candidate_count: int = 0
    suggestion_count: int = 0
    matched_count: int = 0       # suggestions that reached AUTO_MATCH_THRESHOLD, unambiguously
    ambiguous_count: int = 0     # bank transactions with 2+ tied top candidates
    unmatched_count: int = 0     # bank transactions with no candidate clearing REVIEW_THRESHOLD


async def _existing_suggestion(
    db: AsyncSession, run_id: uuid.UUID, bank_statement_transaction_id: uuid.UUID,
    treasury_transaction_id,
) -> ReconciliationMatchSuggestion | None:
    """SECTION 27: application-level idempotency check, backed at the database level by
    ux_reconciliation_match_suggestions_identity (see the Stage 5C migration)."""
    stmt = select(ReconciliationMatchSuggestion).where(
        ReconciliationMatchSuggestion.reconciliation_run_id == run_id,
        ReconciliationMatchSuggestion.bank_statement_transaction_id == bank_statement_transaction_id,
        ReconciliationMatchSuggestion.treasury_transaction_id == treasury_transaction_id
        if treasury_transaction_id is not None
        else ReconciliationMatchSuggestion.treasury_transaction_id.is_(None),
    )
    return (await db.execute(stmt)).scalars().first()


async def _bank_transaction_already_processed(
    db: AsyncSession, run_id: uuid.UUID, bank_statement_transaction_id: uuid.UUID,
) -> bool:
    """
    SECTION 27/29 (idempotency bug fix): true if THIS run already has
    ANY suggestion at all for this bank transaction - regardless of
    what it references. This must be checked BEFORE candidate
    generation, not only compared against the specific candidate a
    second pass happens to land on: re-running the same run's matching
    pass can legitimately see a DIFFERENT candidate population the
    second time (e.g. the correct match from pass 1 is now excluded by
    already_claimed_treasury_transaction_ids, since it has its own live
    PENDING suggestion) - without this early, run-scoped guard, a
    second pass would conclude "no candidate found" and create a
    SECOND, spurious null-treasury suggestion for the same bank
    transaction instead of recognizing it was already processed.
    """
    stmt = select(ReconciliationMatchSuggestion.id).where(
        ReconciliationMatchSuggestion.reconciliation_run_id == run_id,
        ReconciliationMatchSuggestion.bank_statement_transaction_id == bank_statement_transaction_id,
    ).limit(1)
    return (await db.execute(stmt)).scalars().first() is not None


async def run_matching_for_bank_transaction(
    db: AsyncSession, run_id: uuid.UUID, bank_txn: BankStatementTransaction,
    configuration: ReconciliationConfiguration | None, claimed_ids: set,
) -> MatchingRunResult:
    """
    Processes exactly one bank statement transaction: generates
    candidates, scores them, ranks deterministically, and persists the
    resulting suggestion(s) - one row for a clear top candidate, N rows
    (one per tied candidate) for a genuine tie, or one null-treasury-side
    row when nothing clears the review threshold. Never creates more
    than one suggestion referencing the SAME (run, bank transaction,
    treasury transaction) triple even if called twice, AND never creates
    a second, DIFFERENT suggestion for a bank transaction this run has
    already produced any suggestion for at all (idempotency - see
    _bank_transaction_already_processed's own docstring for the bug this
    guards against).
    """
    result = MatchingRunResult()

    if await _bank_transaction_already_processed(db, run_id, bank_txn.id):
        return result

    amount_tolerance_pct = configuration.amount_tolerance_pct if configuration else Decimal(0)
    date_tolerance_days = configuration.date_tolerance_days if configuration else 0
    high_value_threshold = configuration.high_value_threshold if configuration else None

    bank_references = [bank_txn.bank_reference, bank_txn.external_transaction_id]

    candidates = await generate_candidates(
        db, legal_entity_id=bank_txn.legal_entity_id, bank_account_id=bank_txn.bank_account_id,
        currency_code=bank_txn.currency_code, bank_entry_type=bank_txn.entry_type,
        bank_transaction_date=bank_txn.transaction_date, bank_amount=bank_txn.amount,
        date_tolerance_days=date_tolerance_days, amount_tolerance_pct=amount_tolerance_pct,
        excluded_treasury_transaction_ids=claimed_ids,
    )
    result.candidate_count = len(candidates)

    scored = []
    for ledger_txn in candidates:
        score = score_candidate(
            bank_amount=bank_txn.amount, ledger_amount=ledger_txn.transaction_amount,
            bank_date=bank_txn.transaction_date, ledger_date=ledger_txn.event_date,
            bank_references=bank_references, ledger_references=[ledger_txn.reference, ledger_txn.external_reference],
            bank_entry_type=bank_txn.entry_type, ledger_direction=ledger_txn.direction,
            bank_narration=bank_txn.narration, ledger_texts=[ledger_txn.narration, ledger_txn.counterparty],
            treasury_transaction_id=ledger_txn.id,
        )
        if score is not None and score.total >= REVIEW_THRESHOLD:
            scored.append((score, ledger_txn))

    # SECTION 50: fully deterministic ranking - score DESC, then the
    # ledger transaction's own event_date ASC, then id ASC as a final,
    # always-unique tiebreaker.
    scored.sort(key=lambda pair: (-pair[0].total, pair[1].event_date, str(pair[1].id)))

    if not scored:
        result.unmatched_count += 1
        existing = await _existing_suggestion(db, run_id, bank_txn.id, None)
        if existing is None:
            db.add(ReconciliationMatchSuggestion(
                reconciliation_run_id=run_id, bank_statement_transaction_id=bank_txn.id,
                treasury_transaction_id=None, match_type=None, confidence=None,
                status=MatchSuggestionStatus.PENDING,
                reason="No eligible TreasuryTransaction candidate met the configured review "
                       "threshold within this run's entity/bank account/currency/date/amount scope.",
                matching_rule_version=MATCHING_RULE_VERSION,
            ))
            result.suggestion_count += 1
        return result

    top_score = scored[0][0].total
    tied = [pair for pair in scored if pair[0].total == top_score]

    if len(tied) > 1:
        result.ambiguous_count += 1
        for score, ledger_txn in tied:
            existing = await _existing_suggestion(db, run_id, bank_txn.id, ledger_txn.id)
            if existing is not None:
                continue
            match_type = MatchType.EXACT if score.is_exact_match else MatchType.TOLERANCE
            reason = (
                f"AMBIGUOUS: {len(tied)} candidates tied at score {top_score} - not "
                f"auto-selected. {score.reason}."
            )
            _add_suggestion(db, run_id, bank_txn, ledger_txn.id, match_type, score.total, reason)
            result.suggestion_count += 1
        return result

    score, ledger_txn = scored[0]
    existing = await _existing_suggestion(db, run_id, bank_txn.id, ledger_txn.id)
    if existing is not None:
        return result

    match_type = MatchType.EXACT if score.is_exact_match else MatchType.TOLERANCE
    tier = "auto-match" if score.total >= AUTO_MATCH_THRESHOLD else "review"
    # SECTION 41: a high-value bank transaction is treated more
    # conservatively - it is only framed as an "auto-match" candidate
    # when it also has a real reference match, never on amount+date+
    # narration alone, since narration is the least reliable signal.
    if (
        high_value_threshold is not None and bank_txn.amount > high_value_threshold
        and score.reference_points == Decimal(0) and tier == "auto-match"
    ):
        tier = "review"
    reason = f"[{tier}] {score.reason}."
    if tier == "auto-match":
        result.matched_count += 1
    _add_suggestion(db, run_id, bank_txn, ledger_txn.id, match_type, score.total, reason)
    result.suggestion_count += 1
    return result


def _add_suggestion(db, run_id, bank_txn, treasury_transaction_id, match_type, confidence, reason) -> None:
    db.add(ReconciliationMatchSuggestion(
        reconciliation_run_id=run_id, bank_statement_transaction_id=bank_txn.id,
        treasury_transaction_id=treasury_transaction_id, match_type=match_type, confidence=confidence,
        status=MatchSuggestionStatus.PENDING, reason=reason, matching_rule_version=MATCHING_RULE_VERSION,
    ))


async def run_matching_for_run(
    db: AsyncSession, run_id: uuid.UUID, legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID,
    period_start: datetime.date, period_end: datetime.date, configuration: ReconciliationConfiguration | None,
) -> MatchingRunResult:
    """
    Processes every BankStatementTransaction in the run's own scope
    (SECTION 5/18) - the exact same scope
    execute_reconciliation_run's own counting query already uses. Claimed
    treasury-transaction IDs (SECTION 39) are computed ONCE up front and
    updated as suggestions are added within this pass, so two bank
    transactions in the SAME run can never both claim the same
    TreasuryTransaction as their top candidate.
    """
    bank_txns_stmt = select(BankStatementTransaction).where(
        BankStatementTransaction.legal_entity_id == legal_entity_id,
        BankStatementTransaction.bank_account_id == bank_account_id,
        BankStatementTransaction.transaction_date >= period_start,
        BankStatementTransaction.transaction_date <= period_end,
    ).order_by(BankStatementTransaction.transaction_date, BankStatementTransaction.id)
    bank_txns = list((await db.execute(bank_txns_stmt)).scalars().all())

    claimed_ids = await already_claimed_treasury_transaction_ids(db, legal_entity_id)

    total = MatchingRunResult()
    for bank_txn in bank_txns:
        per_txn_result = await run_matching_for_bank_transaction(db, run_id, bank_txn, configuration, claimed_ids)
        total.candidate_count += per_txn_result.candidate_count
        total.suggestion_count += per_txn_result.suggestion_count
        total.matched_count += per_txn_result.matched_count
        total.ambiguous_count += per_txn_result.ambiguous_count
        total.unmatched_count += per_txn_result.unmatched_count

        # Update the claimed set with whatever this bank transaction just
        # claimed (a clear, unambiguous match only - an ambiguous tie
        # claims nothing, since no single candidate was actually selected).
        claimed_stmt = select(ReconciliationMatchSuggestion.treasury_transaction_id).where(
            ReconciliationMatchSuggestion.reconciliation_run_id == run_id,
            ReconciliationMatchSuggestion.bank_statement_transaction_id == bank_txn.id,
            ReconciliationMatchSuggestion.treasury_transaction_id.is_not(None),
        )
        newly_claimed = {row[0] for row in (await db.execute(claimed_stmt)).all()}
        if len(newly_claimed) == 1:
            claimed_ids |= newly_claimed

    await db.flush()
    return total
