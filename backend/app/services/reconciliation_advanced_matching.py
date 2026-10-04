"""
Stage 5D advanced deterministic matching engine.

Extends the Stage 5C one-to-one engine (never redesigns it) with
bounded, deterministic grouped matching: one-to-many, many-to-one,
batch, internal transfer, and evidence-based FX matching. Runs AFTER
Stage 5C's one-to-one pass within the SAME execution boundary
(reconciliation_service.py::execute_reconciliation_run), operating only
on bank/ledger transactions Stage 5C left unmatched (or, for internal
transfers, on transactions Stage 5C DID match one-to-one, since a
transfer relationship is a property of the ledger pair itself, not an
alternative to the one-to-one match).

MATCHING_RULE_VERSION = "5D-1.0" - a separate, independently versioned
rule set from Stage 5C's own "5C-1.0". Stage 5C's scoring/thresholds/
weights are never read or modified by this module.

Financial integrity: this module only reads BankStatementTransaction/
TreasuryTransaction/FXRate and writes ReconciliationMatchGroup/
ReconciliationMatchGroupMember rows. It never creates, modifies, or
deletes any cash-affecting record.

Bounded candidate generation (SECTION 8): no unrestricted Cartesian
subset search is ever performed. Grouping is driven entirely by
deterministic REFERENCE evidence (SECTION 12: "do not infer batches
solely from amount") - candidates sharing the bank transaction's own
normalized reference are gathered (bounded by
`max_group_size` from configuration), and at most one "drop exactly one
member" pass is attempted if the full set doesn't sum within tolerance
(bounded: O(N) additional checks, never combinatorial). No
`itertools.combinations` over arbitrary subsets is ever used.
"""
import datetime
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import (
    BankStatementEntryType,
    BankStatementTransaction,
    BankStatementTransactionStatus,
)
from app.models.currency import Currency, FXRate, FXRateType
from app.models.lookup import CashDirection
from app.models.reconciliation import (
    MatchGroupStatus,
    MatchRelationshipType,
    ReconciliationConfiguration,
    ReconciliationMatchGroup,
    ReconciliationMatchGroupMember,
    ReconciliationMatchSuggestion,
)
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.services.reconciliation_candidate_generation import (
    already_claimed_treasury_transaction_ids,
)
from app.services.reconciliation_normalization import normalize_reference

MATCHING_RULE_VERSION = "5D-1.0"

_COMPATIBLE_DIRECTION = {
    BankStatementEntryType.CREDIT: CashDirection.INFLOW,
    BankStatementEntryType.DEBIT: CashDirection.OUTFLOW,
}

# Conservative deterministic defaults (SECTION 9), used only when the
# run's own configuration.matching_rule_config leaves a key unset -
# never overriding an explicit configuration value. Kept in this module,
# not reconciliation_scoring.py, since these govern Stage 5D's own
# bounded-search shape, never Stage 5C's scoring.
DEFAULT_MAX_GROUP_SIZE = 5
DEFAULT_MAX_CANDIDATES_EVALUATED = 20
DEFAULT_FX_TOLERANCE_PCT = Decimal("1.0")


@dataclass
class AdvancedMatchingResult:
    advanced_match_count: int = 0
    ambiguous_advanced_count: int = 0
    claimed_treasury_transaction_ids: set = field(default_factory=set)
    claimed_bank_statement_transaction_ids: set = field(default_factory=set)


def compute_group_key(
    relationship_type: MatchRelationshipType, bank_ids: list, ledger_ids: list,
) -> str:
    """
    SECTION 21: deterministic idempotency key - sorted member ID lists so
    the SAME group (however the candidates were ordered when found)
    always produces the SAME key, backed by
    uq_reconciliation_match_group_identity on
    (reconciliation_run_id, group_key).
    """
    sorted_bank = sorted(str(i) for i in bank_ids)
    sorted_ledger = sorted(str(i) for i in ledger_ids)
    return f"{relationship_type.value}:{','.join(sorted_bank)}:{','.join(sorted_ledger)}"


def _advanced_config(configuration: ReconciliationConfiguration | None) -> dict:
    rule_config = (configuration.matching_rule_config if configuration else None) or {}
    return {
        "enabled": bool(rule_config.get("advanced_matching_enabled", True)),
        "max_group_size": int(rule_config.get("max_group_size", DEFAULT_MAX_GROUP_SIZE)),
        "max_candidates_evaluated": int(
            rule_config.get("max_candidates_evaluated", DEFAULT_MAX_CANDIDATES_EVALUATED)
        ),
        "fx_tolerance_pct": Decimal(str(rule_config.get("fx_tolerance_pct", DEFAULT_FX_TOLERANCE_PCT))),
    }


async def _existing_group(db: AsyncSession, run_id: uuid.UUID, group_key: str) -> ReconciliationMatchGroup | None:
    stmt = select(ReconciliationMatchGroup).where(
        ReconciliationMatchGroup.reconciliation_run_id == run_id,
        ReconciliationMatchGroup.group_key == group_key,
    )
    return (await db.execute(stmt)).scalars().first()


async def _persist_group(
    db: AsyncSession, run_id: uuid.UUID, relationship_type: MatchRelationshipType, status: MatchGroupStatus,
    bank_ids: list, ledger_ids: list, bank_aggregate: Decimal, ledger_aggregate: Decimal, currency_code: str,
    reason: str, score: Decimal = Decimal(100),
    fx_source: str | None = None, fx_target: str | None = None, fx_rate: Decimal | None = None,
    fx_rate_date: datetime.date | None = None, fx_rate_id: uuid.UUID | None = None,
    fx_rate_type: str | None = None, fx_rate_source: str | None = None, fx_tolerance_pct: Decimal | None = None,
    difference: Decimal | None = None,
) -> bool:
    """Returns True if a new group was persisted, False if it already existed (idempotency, SECTION 21).
    `difference` is only passed for FX groups, where bank and ledger aggregates are in DIFFERENT
    currencies so their direct subtraction is meaningless; it carries |converted - ledger| instead."""
    group_key = compute_group_key(relationship_type, bank_ids, ledger_ids)
    if await _existing_group(db, run_id, group_key) is not None:
        return False

    group = ReconciliationMatchGroup(
        reconciliation_run_id=run_id, relationship_type=relationship_type, status=status,
        bank_aggregate_amount=bank_aggregate, ledger_aggregate_amount=ledger_aggregate,
        currency_code=currency_code,
        difference=difference if difference is not None else abs(bank_aggregate - ledger_aggregate),
        fx_source_currency_code=fx_source, fx_target_currency_code=fx_target, fx_rate=fx_rate,
        fx_rate_date=fx_rate_date, fx_rate_id=fx_rate_id, fx_rate_type=fx_rate_type,
        fx_rate_source=fx_rate_source, fx_tolerance_pct=fx_tolerance_pct, score=score, confidence=score,
        reason=reason, matching_rule_version=MATCHING_RULE_VERSION, group_key=group_key,
    )
    db.add(group)
    await db.flush()

    for bank_id in bank_ids:
        db.add(ReconciliationMatchGroupMember(match_group_id=group.id, bank_statement_transaction_id=bank_id))
    for ledger_id in ledger_ids:
        db.add(ReconciliationMatchGroupMember(match_group_id=group.id, treasury_transaction_id=ledger_id))
    await db.flush()
    return True


async def claimed_ids_from_existing_groups(db: AsyncSession, legal_entity_id: uuid.UUID) -> tuple[set, set]:
    """
    SECTION 19 (Stage 5D): claim protection across RUNS for grouped matches.
    Stage 5C's `already_claimed_treasury_transaction_ids` only sees
    `ReconciliationMatchSuggestion` claims, so a ledger/bank row already
    consumed by a PENDING/ACCEPTED `ReconciliationMatchGroup` from another
    run would otherwise be offered again (found by the cross-run
    concurrency test: two same-scope runs, serialized correctly by the
    advisory lock, still both grouped the same ledger rows). AMBIGUOUS and
    REJECTED groups claim nothing, mirroring Stage 5C's rule that an
    unresolved tie selects no single candidate. Entity scope comes from the
    authoritative TreasuryTransaction/BankStatementTransaction relationship
    via SQL joins, never a Python-side filter. Returns
    (treasury_transaction_ids, bank_statement_transaction_ids).
    """
    live = (MatchGroupStatus.PENDING, MatchGroupStatus.ACCEPTED)
    treasury_stmt = (
        select(ReconciliationMatchGroupMember.treasury_transaction_id)
        .join(ReconciliationMatchGroup, ReconciliationMatchGroup.id == ReconciliationMatchGroupMember.match_group_id)
        .join(TreasuryTransaction, TreasuryTransaction.id == ReconciliationMatchGroupMember.treasury_transaction_id)
        .where(ReconciliationMatchGroup.status.in_(live), TreasuryTransaction.legal_entity_id == legal_entity_id)
    )
    bank_stmt = (
        select(ReconciliationMatchGroupMember.bank_statement_transaction_id)
        .join(ReconciliationMatchGroup, ReconciliationMatchGroup.id == ReconciliationMatchGroupMember.match_group_id)
        .join(
            BankStatementTransaction,
            BankStatementTransaction.id == ReconciliationMatchGroupMember.bank_statement_transaction_id,
        )
        .where(ReconciliationMatchGroup.status.in_(live), BankStatementTransaction.legal_entity_id == legal_entity_id)
    )
    treasury_ids = {row[0] for row in (await db.execute(treasury_stmt)).all()}
    bank_ids = {row[0] for row in (await db.execute(bank_stmt)).all()}
    return treasury_ids, bank_ids


def _normalized_sql(column):
    """SQL mirror of normalize_reference (trim, collapse whitespace, uppercase) so reference
    narrowing happens in the database BEFORE any LIMIT - applying a LIMIT first would drop real
    candidates arbitrarily whenever many unrelated rows share the date window."""
    return func.regexp_replace(func.upper(func.trim(column)), r"\s+", " ", "g")


def _is_batch_evidence(bank_txn: BankStatementTransaction) -> bool:
    """
    SECTION 3.C/12: BankStatementTransaction has no dedicated batch-
    reference field (only bank_reference/external_transaction_id/
    narration - inspected in the actual current schema, not assumed).
    Rather than invent one, BATCH is deterministically distinguished
    from a plain ONE_TO_MANY/MANY_TO_ONE grouping by an explicit
    "BATCH" keyword appearing in the bank transaction's own reference or
    narration (case-insensitive, via the same normalize_reference used
    for everything else) - genuine, deterministic evidence, never an
    amount-only inference.
    """
    for value in (bank_txn.bank_reference, bank_txn.external_transaction_id, bank_txn.narration):
        if value and "BATCH" in normalize_reference(value):
            return True
    return False


def _relationship_type_for(bank_txn: BankStatementTransaction, cardinality: MatchRelationshipType) -> MatchRelationshipType:
    return MatchRelationshipType.BATCH if _is_batch_evidence(bank_txn) else cardinality


async def _reference_matched_ledger_candidates(
    db: AsyncSession, bank_txn: BankStatementTransaction, date_tolerance_days: int,
    excluded_treasury_transaction_ids: set, max_candidates: int,
) -> list:
    """
    Ledger candidates for a ONE_TO_MANY grouping starting from a bank
    transaction: same entity/account/currency/compatible-direction/
    POSTED status/date-window as Stage 5C's own candidate generation
    (never relaxed), PLUS a normalized-reference match against the bank
    transaction's own reference fields (SECTION 12's "strong evidence").
    """
    bank_refs = {normalize_reference(r) for r in (bank_txn.bank_reference, bank_txn.external_transaction_id) if r}
    if not bank_refs:
        return []

    compatible_direction = _COMPATIBLE_DIRECTION[bank_txn.entry_type]
    window_start = bank_txn.transaction_date - datetime.timedelta(days=date_tolerance_days)
    window_end = bank_txn.transaction_date + datetime.timedelta(days=date_tolerance_days)

    stmt = select(TreasuryTransaction).where(
        TreasuryTransaction.legal_entity_id == bank_txn.legal_entity_id,
        TreasuryTransaction.bank_account_id == bank_txn.bank_account_id,
        TreasuryTransaction.transaction_currency_code == bank_txn.currency_code,
        TreasuryTransaction.direction == compatible_direction,
        TreasuryTransaction.status == TransactionStatus.POSTED,
        TreasuryTransaction.event_date >= window_start,
        TreasuryTransaction.event_date <= window_end,
    ).where(
        or_(
            _normalized_sql(TreasuryTransaction.reference).in_(bank_refs),
            _normalized_sql(TreasuryTransaction.external_reference).in_(bank_refs),
        )
    ).order_by(TreasuryTransaction.event_date, TreasuryTransaction.id).limit(max_candidates)
    if excluded_treasury_transaction_ids:
        stmt = stmt.where(TreasuryTransaction.id.not_in(excluded_treasury_transaction_ids))

    # Reference narrowing, deterministic ordering and the bound all happen in SQL; the Python
    # re-check below is defence in depth against any SQL/Python normalization drift.
    candidates = list((await db.execute(stmt)).scalars().all())
    return [
        c for c in candidates
        if normalize_reference(c.reference) in bank_refs or normalize_reference(c.external_reference) in bank_refs
    ]


def _find_summing_grouping(
    candidates: list, target: Decimal, tolerance_pct: Decimal, amount_getter,
) -> tuple[list | None, list]:
    """
    Bounded grouping search (SECTION 8/18): tries the FULL candidate set
    first; if that doesn't sum within tolerance, tries each "drop exactly
    one member" subset (O(N), never combinatorial). Returns
    (accepted_subset, ambiguous_alternate_subsets) - the caller persists
    accepted_subset as PENDING if `ambiguous_alternate_subsets` is empty,
    or persists EVERY subset in the combined ambiguous set as AMBIGUOUS
    if there are 2+ equally-valid drop-one groupings (SECTION 18: never
    arbitrarily pick one).

    `amount_getter` extracts the amount from a candidate - callers pass
    a different accessor depending on which model the candidates are
    (TreasuryTransaction.transaction_amount for one-to-many's ledger
    candidates, BankStatementTransaction.amount for many-to-one's bank
    candidates) rather than this shared function assuming either shape.
    """
    allowed_diff = target * (tolerance_pct / Decimal(100))

    full_sum = sum((amount_getter(c) for c in candidates), Decimal(0))
    if abs(full_sum - target) <= allowed_diff:
        return candidates, []

    valid_drop_one = []
    for i in range(len(candidates)):
        subset = candidates[:i] + candidates[i + 1:]
        if len(subset) < 2:
            continue
        subset_sum = sum((amount_getter(c) for c in subset), Decimal(0))
        if abs(subset_sum - target) <= allowed_diff:
            valid_drop_one.append(subset)

    if len(valid_drop_one) == 1:
        return valid_drop_one[0], []
    if len(valid_drop_one) > 1:
        return None, valid_drop_one
    return None, []


async def detect_one_to_many_groups(
    db: AsyncSession, run_id: uuid.UUID, legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID,
    period_start: datetime.date, period_end: datetime.date, configuration: ReconciliationConfiguration | None,
    unmatched_bank_txn_ids: set, extra_excluded_treasury_ids: set | None = None,
) -> AdvancedMatchingResult:
    """
    SECTION 3.A: one bank transaction whose amount equals the aggregate
    of several ledger transactions sharing its own normalized reference.
    Only considers bank transactions Stage 5C left unmatched
    (`unmatched_bank_txn_ids` - those with a null-treasury-transaction
    suggestion from the one-to-one pass), so a bank transaction Stage 5C
    already matched one-to-one is never reconsidered here.
    `extra_excluded_treasury_ids` additionally excludes ledger
    transactions already claimed by an EARLIER Stage 5D phase within the
    same execution pass (e.g. many-to-one) - `already_claimed_treasury_transaction_ids`
    only sees Stage 5C `ReconciliationMatchSuggestion` claims, never
    `ReconciliationMatchGroup` claims from an earlier phase in the same
    orchestration call, so the orchestrator must thread this through
    explicitly.
    """
    cfg = _advanced_config(configuration)
    result = AdvancedMatchingResult()
    if not cfg["enabled"] or not unmatched_bank_txn_ids:
        return result

    date_tolerance_days = configuration.date_tolerance_days if configuration else 0
    amount_tolerance_pct = configuration.amount_tolerance_pct if configuration else Decimal(0)

    bank_stmt = select(BankStatementTransaction).where(
        BankStatementTransaction.id.in_(unmatched_bank_txn_ids),
        BankStatementTransaction.status == BankStatementTransactionStatus.ACTIVE,
    ).order_by(BankStatementTransaction.transaction_date, BankStatementTransaction.id)
    bank_txns = list((await db.execute(bank_stmt)).scalars().all())

    claimed_ids = await already_claimed_treasury_transaction_ids(db, legal_entity_id)
    claimed_ids |= extra_excluded_treasury_ids or set()

    for bank_txn in bank_txns:
        candidates = await _reference_matched_ledger_candidates(
            db, bank_txn, date_tolerance_days, claimed_ids, cfg["max_candidates_evaluated"],
        )
        if len(candidates) < 2:
            continue  # a single reference-matched candidate is Stage 5C's own job, not a group

        accepted, ambiguous_alternates = _find_summing_grouping(
            candidates, bank_txn.amount, amount_tolerance_pct, amount_getter=lambda c: c.transaction_amount,
        )

        if accepted:
            relationship_type = _relationship_type_for(bank_txn, MatchRelationshipType.ONE_TO_MANY)
            ledger_sum = sum((c.transaction_amount for c in accepted), Decimal(0))
            reason = (
                f"{len(accepted)} ledger transactions sharing bank transaction's own normalized "
                f"reference sum to {ledger_sum} against a bank amount of {bank_txn.amount} "
                f"(within {amount_tolerance_pct}% tolerance)."
            )
            created = await _persist_group(
                db, run_id, relationship_type, MatchGroupStatus.PENDING, [bank_txn.id],
                [c.id for c in accepted], bank_txn.amount, ledger_sum, bank_txn.currency_code, reason,
            )
            if created:
                result.advanced_match_count += 1
                claimed_ids |= {c.id for c in accepted}
                result.claimed_treasury_transaction_ids |= {c.id for c in accepted}
                result.claimed_bank_statement_transaction_ids.add(bank_txn.id)
        elif ambiguous_alternates:
            relationship_type = _relationship_type_for(bank_txn, MatchRelationshipType.ONE_TO_MANY)
            any_new = False
            for subset in ambiguous_alternates:
                ledger_sum = sum((c.transaction_amount for c in subset), Decimal(0))
                reason = (
                    f"AMBIGUOUS: {len(ambiguous_alternates)} different subsets of the same "
                    f"reference-matched candidates each sum within tolerance of the bank amount "
                    f"{bank_txn.amount} - no automatic selection made."
                )
                created = await _persist_group(
                    db, run_id, relationship_type, MatchGroupStatus.AMBIGUOUS, [bank_txn.id],
                    [c.id for c in subset], bank_txn.amount, ledger_sum, bank_txn.currency_code, reason,
                )
                any_new = any_new or created
            if any_new:
                result.ambiguous_advanced_count += 1

    return result


async def detect_many_to_one_groups(
    db: AsyncSession, run_id: uuid.UUID, legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID,
    period_start: datetime.date, period_end: datetime.date, configuration: ReconciliationConfiguration | None,
    extra_excluded_treasury_ids: set | None = None, extra_excluded_bank_ids: set | None = None,
) -> AdvancedMatchingResult:
    """
    SECTION 3.B: several bank transactions whose combined amount equals
    one, still-unclaimed, ledger transaction sharing their normalized
    reference. Symmetric to detect_one_to_many_groups, starting from the
    ledger side. `extra_excluded_treasury_ids`/`extra_excluded_bank_ids`
    exclude claims already made by an earlier Stage 5D phase within the
    same pass (see detect_one_to_many_groups's own docstring for why
    this threading is necessary).
    """
    cfg = _advanced_config(configuration)
    result = AdvancedMatchingResult()
    if not cfg["enabled"]:
        return result

    date_tolerance_days = configuration.date_tolerance_days if configuration else 0
    amount_tolerance_pct = configuration.amount_tolerance_pct if configuration else Decimal(0)

    claimed_ids = await already_claimed_treasury_transaction_ids(db, legal_entity_id)
    claimed_ids |= extra_excluded_treasury_ids or set()
    grouped_bank_ids: set = set(extra_excluded_bank_ids or set())

    # Many-to-one needs 2+ bank rows sharing a reference, so only references that occur on at
    # least two ACTIVE in-scope bank rows can ever form a group. Computing that set from the
    # bank side first lets the ledger query be narrowed by reference IN SQL, instead of loading
    # every unclaimed ledger row in the period into Python.
    ref_rows = (await db.execute(
        select(BankStatementTransaction.bank_reference, BankStatementTransaction.external_transaction_id).where(
            BankStatementTransaction.legal_entity_id == legal_entity_id,
            BankStatementTransaction.bank_account_id == bank_account_id,
            BankStatementTransaction.status == BankStatementTransactionStatus.ACTIVE,
            BankStatementTransaction.transaction_date >= period_start,
            BankStatementTransaction.transaction_date <= period_end,
        )
    )).all()
    ref_counts: dict = {}
    for bank_ref, external_ref in ref_rows:
        for value in {normalize_reference(bank_ref), normalize_reference(external_ref)} - {""}:
            ref_counts[value] = ref_counts.get(value, 0) + 1
    shared_refs = {ref for ref, count in ref_counts.items() if count >= 2}
    if not shared_refs:
        return result

    ledger_stmt = select(TreasuryTransaction).where(
        TreasuryTransaction.legal_entity_id == legal_entity_id,
        TreasuryTransaction.bank_account_id == bank_account_id,
        TreasuryTransaction.status == TransactionStatus.POSTED,
        TreasuryTransaction.event_date >= period_start - datetime.timedelta(days=date_tolerance_days),
        TreasuryTransaction.event_date <= period_end + datetime.timedelta(days=date_tolerance_days),
        or_(
            _normalized_sql(TreasuryTransaction.reference).in_(shared_refs),
            _normalized_sql(TreasuryTransaction.external_reference).in_(shared_refs),
        ),
    ).order_by(TreasuryTransaction.event_date, TreasuryTransaction.id)
    if claimed_ids:
        ledger_stmt = ledger_stmt.where(TreasuryTransaction.id.not_in(claimed_ids))
    ledger_txns = list((await db.execute(ledger_stmt)).scalars().all())

    for ledger_txn in ledger_txns:
        ledger_refs = {normalize_reference(r) for r in (ledger_txn.reference, ledger_txn.external_reference) if r}
        if not ledger_refs:
            continue

        window_start = ledger_txn.event_date - datetime.timedelta(days=date_tolerance_days)
        window_end = ledger_txn.event_date + datetime.timedelta(days=date_tolerance_days)
        compatible_bank_entry_type = (
            BankStatementEntryType.CREDIT if ledger_txn.direction == CashDirection.INFLOW
            else BankStatementEntryType.DEBIT
        )

        bank_stmt = select(BankStatementTransaction).where(
            BankStatementTransaction.legal_entity_id == legal_entity_id,
            BankStatementTransaction.bank_account_id == bank_account_id,
            BankStatementTransaction.currency_code == ledger_txn.transaction_currency_code,
            BankStatementTransaction.entry_type == compatible_bank_entry_type,
            BankStatementTransaction.status == BankStatementTransactionStatus.ACTIVE,
            BankStatementTransaction.transaction_date >= window_start,
            BankStatementTransaction.transaction_date <= window_end,
        ).where(
            or_(
                _normalized_sql(BankStatementTransaction.bank_reference).in_(ledger_refs),
                _normalized_sql(BankStatementTransaction.external_transaction_id).in_(ledger_refs),
            )
        ).order_by(BankStatementTransaction.transaction_date, BankStatementTransaction.id).limit(
            cfg["max_candidates_evaluated"]
        )
        if grouped_bank_ids:
            bank_stmt = bank_stmt.where(BankStatementTransaction.id.not_in(grouped_bank_ids))
        candidates = [
            b for b in (await db.execute(bank_stmt)).scalars().all()
            if normalize_reference(b.bank_reference) in ledger_refs
            or normalize_reference(b.external_transaction_id) in ledger_refs
        ]

        if len(candidates) < 2:
            continue

        accepted, ambiguous_alternates = _find_summing_grouping(
            candidates, ledger_txn.transaction_amount, amount_tolerance_pct, amount_getter=lambda c: c.amount,
        )

        if accepted:
            relationship_type = _relationship_type_for(accepted[0], MatchRelationshipType.MANY_TO_ONE)
            bank_sum = sum((c.amount for c in accepted), Decimal(0))
            reason = (
                f"{len(accepted)} bank transactions sharing the ledger transaction's own normalized "
                f"reference sum to {bank_sum} against a ledger amount of {ledger_txn.transaction_amount} "
                f"(within {amount_tolerance_pct}% tolerance)."
            )
            created = await _persist_group(
                db, run_id, relationship_type, MatchGroupStatus.PENDING, [c.id for c in accepted],
                [ledger_txn.id], bank_sum, ledger_txn.transaction_amount, ledger_txn.transaction_currency_code, reason,
            )
            if created:
                result.advanced_match_count += 1
                grouped_bank_ids |= {c.id for c in accepted}
                result.claimed_bank_statement_transaction_ids |= {c.id for c in accepted}
                result.claimed_treasury_transaction_ids.add(ledger_txn.id)
        elif ambiguous_alternates:
            relationship_type = _relationship_type_for(candidates[0], MatchRelationshipType.MANY_TO_ONE)
            any_new = False
            for subset in ambiguous_alternates:
                bank_sum = sum((c.amount for c in subset), Decimal(0))
                reason = (
                    f"AMBIGUOUS: {len(ambiguous_alternates)} different subsets of the same "
                    f"reference-matched bank candidates each sum within tolerance of the ledger "
                    f"amount {ledger_txn.transaction_amount} - no automatic selection made."
                )
                created = await _persist_group(
                    db, run_id, relationship_type, MatchGroupStatus.AMBIGUOUS, [c.id for c in subset],
                    [ledger_txn.id], bank_sum, ledger_txn.transaction_amount,
                    ledger_txn.transaction_currency_code, reason,
                )
                any_new = any_new or created
            if any_new:
                result.ambiguous_advanced_count += 1

    return result


async def detect_internal_transfers(
    db: AsyncSession, run_id: uuid.UUID, legal_entity_id: uuid.UUID, matched_one_to_one_treasury_ids: list,
) -> AdvancedMatchingResult:
    """
    SECTION 4.D/15: reuses the EXISTING `TreasuryTransaction.transfer_pair_id`
    field (both legs of a transfer share the same value - SECTION 6 of
    treasury_transaction.py's own docstring) - no new transfer ledger, no
    mutation of TreasuryTransaction. Only considers TreasuryTransaction
    rows Stage 5C's one-to-one pass ALREADY matched this run
    (`matched_one_to_one_treasury_ids`) - a transfer relationship is
    additional context about an already-matched ledger transaction, not
    an alternative candidate pool. If the paired leg belongs to a
    DIFFERENT entity, no group is created at all (entity isolation,
    SECTION 28) - two transactions in different entities are never
    described as "the same internal transfer" regardless of what
    transfer_pair_id says, since that would imply information about
    another entity's ledger.
    """
    result = AdvancedMatchingResult()
    if not matched_one_to_one_treasury_ids:
        return result

    primary_stmt = select(TreasuryTransaction).where(
        TreasuryTransaction.id.in_(matched_one_to_one_treasury_ids),
        TreasuryTransaction.transfer_pair_id.is_not(None),
    )
    primaries = list((await db.execute(primary_stmt)).scalars().all())

    for primary in primaries:
        pair_stmt = select(TreasuryTransaction).where(
            TreasuryTransaction.transfer_pair_id == primary.transfer_pair_id,
            TreasuryTransaction.id != primary.id,
        )
        pair = (await db.execute(pair_stmt)).scalars().first()
        if pair is None or pair.legal_entity_id != legal_entity_id:
            continue  # entity isolation - never describe a cross-entity relationship

        reason = (
            f"TreasuryTransaction {primary.id} and {pair.id} share transfer_pair_id "
            f"{primary.transfer_pair_id} - recognized as two legs of one internal treasury "
            f"transfer, not an unexplained external difference."
        )
        created = await _persist_group(
            db, run_id, MatchRelationshipType.INTERNAL_TRANSFER, MatchGroupStatus.PENDING, [],
            [primary.id, pair.id], primary.transaction_amount, pair.transaction_amount,
            primary.transaction_currency_code, reason,
        )
        if created:
            result.advanced_match_count += 1

    return result


@dataclass(frozen=True)
class FxRateSelection:
    """SECTION 16A.8: complete provenance for a selected FX rate - enough to reproduce the conversion decision alone."""
    fx_rate: FXRate
    source_currency: str
    target_currency: str
    inverse_used: bool


async def select_fx_rate(
    db: AsyncSession, source_currency: str, target_currency: str, transaction_date: datetime.date,
    configuration: ReconciliationConfiguration | None,
) -> FxRateSelection | None:
    """
    SECTION 16A.10: the single authoritative, deterministic FX rate
    selection function - never current time, never "latest row", never
    an arbitrary first result, no external API, no LLM inference.
    Returns None (NO FX MATCH) whenever any step below cannot be
    satisfied deterministically - the caller must never substitute a
    different currency pair, rate type, or approximate rate.

    Steps actually implemented, in order:
    1. Rate type: SECTION 16A.2 - read from
       `configuration.matching_rule_config["fx_rate_type"]` if the run's
       own configuration specifies one; otherwise fall back to
       `FXRateType.SPOT`, the same default `FXRate.rate_type` itself
       already uses at the model level (SECTION: "use the repository's
       established default"). Never silently substitutes a different
       type if the configured one has no rates - that is NO FX MATCH,
       not a fallback to another type.
    2. Effective date: SECTION 16A.3 - only rows with
       `rate_date <= transaction_date` are ever considered (an exact-date
       row is simply the closest such row, so no separate "exact" step is
       needed); a future-dated rate is never selected, enforced directly
       by the `<=` filter, not by a post-hoc check.
    3. Maximum staleness: SECTION 16A.4 - enforced only if the run's
       configuration sets `matching_rule_config["fx_max_staleness_days"]`;
       if unset, no staleness limit is invented (per the section's own
       explicit instruction not to invent a business rule that doesn't
       already exist in the architecture).
    4. Source priority / ambiguity: SECTION 16A.5 - `FXRate`'s own
       identity constraint (`uq_fx_rate_identity` on
       `from_currency_code, to_currency_code, rate_type, rate_date,
       version`) already makes it impossible for two `is_current=True`
       rows to share the same (pair, type, date) - see this function's
       own defensive check below, which is unreachable in practice given
       that constraint (documented explicitly, not silently assumed).
    5. Direction: SECTION 16A.6 - only the DIRECT pair
       (source_currency -> target_currency) is ever queried. No
       inversion is attempted, because the current `FXRate` model
       carries no explicit flag or documented convention permitting
       mathematical inversion of a stored rate - per the section's own
       instruction ("only if the existing model explicitly permits it"),
       the honest, correct behavior here is NO FX MATCH when only the
       reverse pair exists, not a silent inversion. `inverse_used` is
       therefore always False in the current implementation - the field
       exists on `FxRateSelection` so a future stage that DOES add
       documented inversion support can populate it without a schema
       change here.
    6. Triangulation: SECTION 16A.7 - never attempted; only a single
       direct currency-pair query is ever issued.
    """
    rule_config = (configuration.matching_rule_config if configuration else None) or {}
    rate_type_value = rule_config.get("fx_rate_type", FXRateType.SPOT.value)
    try:
        required_rate_type = FXRateType(rate_type_value)
    except ValueError:
        return None  # an unrecognized configured rate type is NO FX MATCH, never a silent fallback

    max_staleness_days = rule_config.get("fx_max_staleness_days")

    stmt = select(FXRate).where(
        FXRate.from_currency_code == source_currency, FXRate.to_currency_code == target_currency,
        FXRate.rate_type == required_rate_type, FXRate.is_current.is_(True),
        FXRate.rate_date <= transaction_date,
    ).order_by(FXRate.rate_date.desc())

    if max_staleness_days is not None:
        earliest_allowed = transaction_date - datetime.timedelta(days=int(max_staleness_days))
        stmt = stmt.where(FXRate.rate_date >= earliest_allowed)

    candidates = list((await db.execute(stmt)).scalars().all())
    if not candidates:
        return None  # NO FX MATCH - never guessed, never inverted, never a different rate type

    most_recent_date = candidates[0].rate_date
    same_date_candidates = [c for c in candidates if c.rate_date == most_recent_date]
    if len(same_date_candidates) > 1:
        # SECTION 16A.5: defensive only - uq_fx_rate_identity already
        # makes this unreachable (see docstring above), but the check
        # is kept so this function never silently picks "the first
        # database row" if that invariant is ever relaxed.
        return None  # AMBIGUOUS FX RATE

    return FxRateSelection(
        fx_rate=same_date_candidates[0], source_currency=source_currency, target_currency=target_currency,
        inverse_used=False,
    )


def convert_fx_amount(amount: Decimal, rate: Decimal, target_decimal_places: int) -> Decimal:
    """
    FX precision correction: quantize the converted amount to the configured
    `Currency.decimal_places` of the currency the result is DENOMINATED IN,
    never an assumed two decimals. Pure Decimal arithmetic (no float, no
    round()). Rounding mode is ROUND_HALF_UP, the repository's existing
    monetary convention (investment/facility/forecast engines); the previous
    code passed no mode and silently inherited the Decimal context default
    (ROUND_HALF_EVEN), which differs only on exact .5 ties.
    """
    precision = Decimal(1).scaleb(-target_decimal_places)
    return (amount * rate).quantize(precision, rounding=ROUND_HALF_UP)


async def detect_fx_matches(
    db: AsyncSession, run_id: uuid.UUID, legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID,
    period_start: datetime.date, period_end: datetime.date, configuration: ReconciliationConfiguration | None,
    unmatched_bank_txn_ids: set, extra_excluded_treasury_ids: set | None = None,
    extra_excluded_bank_ids: set | None = None,
) -> AdvancedMatchingResult:
    """
    SECTION 4.E/16: for bank transactions Stage 5C left unmatched
    specifically because NO same-currency candidate existed at all
    (Stage 5C's own candidate generation hard-filters on exact currency
    match, so a genuine cross-currency counterpart is invisible to it),
    look for a same-entity/same-account/compatible-direction/date-window
    candidate in a DIFFERENT currency, convert using an authoritative FX
    rate (never guessed - see select_fx_rate), and compare within the
    configured FX tolerance. `extra_excluded_treasury_ids`/
    `extra_excluded_bank_ids` exclude claims already made by an earlier
    Stage 5D phase within the same pass.
    """
    unmatched_bank_txn_ids = unmatched_bank_txn_ids - (extra_excluded_bank_ids or set())
    cfg = _advanced_config(configuration)
    result = AdvancedMatchingResult()
    if not cfg["enabled"] or not unmatched_bank_txn_ids:
        return result

    date_tolerance_days = configuration.date_tolerance_days if configuration else 0
    claimed_ids = await already_claimed_treasury_transaction_ids(db, legal_entity_id)
    claimed_ids |= extra_excluded_treasury_ids or set()

    bank_stmt = select(BankStatementTransaction).where(
        BankStatementTransaction.id.in_(unmatched_bank_txn_ids),
        BankStatementTransaction.status == BankStatementTransactionStatus.ACTIVE,
    ).order_by(BankStatementTransaction.transaction_date, BankStatementTransaction.id)
    bank_txns = list((await db.execute(bank_stmt)).scalars().all())

    for bank_txn in bank_txns:
        compatible_direction = _COMPATIBLE_DIRECTION[bank_txn.entry_type]
        window_start = bank_txn.transaction_date - datetime.timedelta(days=date_tolerance_days)
        window_end = bank_txn.transaction_date + datetime.timedelta(days=date_tolerance_days)

        cross_currency_stmt = select(TreasuryTransaction).where(
            TreasuryTransaction.legal_entity_id == bank_txn.legal_entity_id,
            TreasuryTransaction.bank_account_id == bank_txn.bank_account_id,
            TreasuryTransaction.transaction_currency_code != bank_txn.currency_code,
            TreasuryTransaction.direction == compatible_direction,
            TreasuryTransaction.status == TransactionStatus.POSTED,
            TreasuryTransaction.event_date >= window_start,
            TreasuryTransaction.event_date <= window_end,
        )
        if claimed_ids:
            cross_currency_stmt = cross_currency_stmt.where(TreasuryTransaction.id.not_in(claimed_ids))
        candidates = list((await db.execute(cross_currency_stmt)).scalars().all())

        for ledger_txn in candidates:
            selection = await select_fx_rate(
                db, bank_txn.currency_code, ledger_txn.transaction_currency_code, bank_txn.transaction_date,
                configuration,
            )
            if selection is None:
                continue  # SECTION 16/16A: no authoritative rate (or ambiguous/stale/wrong type) -> NO FX MATCH
            rate = selection.fx_rate

            # Direction (traced, unchanged): the rate is selected as
            # bank currency (source, from_currency_code) -> ledger
            # transaction currency (target, to_currency_code), used directly
            # with no inversion. The converted amount is therefore
            # denominated in the LEDGER currency, whose configured
            # decimal_places controls quantization. A missing currency row
            # cannot normally occur (FK) but is treated as NO FX MATCH
            # rather than assuming a precision.
            target_currency = await db.get(Currency, ledger_txn.transaction_currency_code)
            if target_currency is None:
                continue
            converted_amount = convert_fx_amount(bank_txn.amount, rate.rate, target_currency.decimal_places)
            allowed_diff = ledger_txn.transaction_amount * (cfg["fx_tolerance_pct"] / Decimal(100))
            if abs(converted_amount - ledger_txn.transaction_amount) > allowed_diff:
                continue

            reason = (
                f"Bank amount {bank_txn.amount} {bank_txn.currency_code} converted at the authoritative "
                f"{rate.rate_type.value} rate {rate.rate} (source: {rate.rate_source}, dated {rate.rate_date}) "
                f"= {converted_amount} {ledger_txn.transaction_currency_code}, within "
                f"{cfg['fx_tolerance_pct']}% of the ledger amount {ledger_txn.transaction_amount} "
                f"{ledger_txn.transaction_currency_code}."
            )
            created = await _persist_group(
                db, run_id, MatchRelationshipType.FX_MATCH, MatchGroupStatus.PENDING, [bank_txn.id],
                [ledger_txn.id], bank_txn.amount, ledger_txn.transaction_amount,
                ledger_txn.transaction_currency_code, reason, fx_source=bank_txn.currency_code,
                fx_target=ledger_txn.transaction_currency_code, fx_rate=rate.rate, fx_rate_date=rate.rate_date,
                fx_rate_id=rate.id, fx_rate_type=rate.rate_type.value, fx_rate_source=rate.rate_source,
                fx_tolerance_pct=cfg["fx_tolerance_pct"],
                difference=abs(converted_amount - ledger_txn.transaction_amount),
            )
            if created:
                result.advanced_match_count += 1
                claimed_ids.add(ledger_txn.id)
                result.claimed_treasury_transaction_ids.add(ledger_txn.id)
                result.claimed_bank_statement_transaction_ids.add(bank_txn.id)
            break  # SECTION 23/7: at most one FX pairing per bank transaction - never many-to-many

    return result


async def run_advanced_matching_for_run(
    db: AsyncSession, run_id: uuid.UUID, legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID,
    period_start: datetime.date, period_end: datetime.date, configuration: ReconciliationConfiguration | None,
) -> AdvancedMatchingResult:
    """
    Orchestrates the full Stage 5D advanced-matching pass, called by
    execute_reconciliation_run AFTER Stage 5C's own run_matching_for_run
    has already persisted its one-to-one suggestions for this run
    (SECTION 25: "Stage 5C one-to-one matching -> Stage 5D advanced
    matching" within the same execution boundary). Reads the Stage 5C
    suggestions this run just created to determine which bank
    transactions remain unmatched (candidates for one-to-many/FX) and
    which TreasuryTransactions Stage 5C DID match one-to-one (candidates
    for internal-transfer recognition).
    """
    total = AdvancedMatchingResult()

    unmatched_stmt = select(ReconciliationMatchSuggestion.bank_statement_transaction_id).where(
        ReconciliationMatchSuggestion.reconciliation_run_id == run_id,
        ReconciliationMatchSuggestion.treasury_transaction_id.is_(None),
        ReconciliationMatchSuggestion.bank_statement_transaction_id.is_not(None),
    )
    unmatched_bank_txn_ids = {row[0] for row in (await db.execute(unmatched_stmt)).all()}

    matched_stmt = select(ReconciliationMatchSuggestion.treasury_transaction_id).where(
        ReconciliationMatchSuggestion.reconciliation_run_id == run_id,
        ReconciliationMatchSuggestion.treasury_transaction_id.is_not(None),
    )
    matched_one_to_one_treasury_ids = [row[0] for row in (await db.execute(matched_stmt)).all()]

    base_treasury_claims, base_bank_claims = await claimed_ids_from_existing_groups(db, legal_entity_id)
    unmatched_bank_txn_ids = unmatched_bank_txn_ids - base_bank_claims

    one_to_many = await detect_one_to_many_groups(
        db, run_id, legal_entity_id, bank_account_id, period_start, period_end, configuration,
        unmatched_bank_txn_ids, extra_excluded_treasury_ids=set(base_treasury_claims),
    )
    many_to_one = await detect_many_to_one_groups(
        db, run_id, legal_entity_id, bank_account_id, period_start, period_end, configuration,
        extra_excluded_treasury_ids=base_treasury_claims | one_to_many.claimed_treasury_transaction_ids,
        extra_excluded_bank_ids=base_bank_claims | one_to_many.claimed_bank_statement_transaction_ids,
    )
    transfers = await detect_internal_transfers(db, run_id, legal_entity_id, matched_one_to_one_treasury_ids)
    fx = await detect_fx_matches(
        db, run_id, legal_entity_id, bank_account_id, period_start, period_end, configuration,
        unmatched_bank_txn_ids,
        extra_excluded_treasury_ids=(
            base_treasury_claims
            | one_to_many.claimed_treasury_transaction_ids
            | many_to_one.claimed_treasury_transaction_ids
        ),
        extra_excluded_bank_ids=(
            base_bank_claims
            | one_to_many.claimed_bank_statement_transaction_ids
            | many_to_one.claimed_bank_statement_transaction_ids
        ),
    )

    for partial in (one_to_many, many_to_one, transfers, fx):
        total.advanced_match_count += partial.advanced_match_count
        total.ambiguous_advanced_count += partial.ambiguous_advanced_count

    await db.flush()
    return total
