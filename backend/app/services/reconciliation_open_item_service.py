"""
Stage 5E: reconciliation open-item workflow.

Two responsibilities, both strictly downstream of the frozen Stage 5A-5D engine:

1. DERIVATION - turn a run's engine output (Stage 5C suggestions + Stage 5D
   match groups) into actionable `ReconciliationOpenItem` rows, deterministically
   and idempotently. An open item is an unresolved exception, never a mere
   suggestion:

   * Stage 5C only ever persists PENDING suggestions (nothing auto-accepts), so
     "matched" is derived from the engine's own tiering: an UNAMBIGUOUS
     one-to-one suggestion in the auto-match tier (confidence >= the Stage 5C
     AUTO_MATCH_THRESHOLD and not downgraded to "[review]" by the high-value
     gate) is treated as matched and creates NO item. A review-tier suggestion
     is actionable (classified AMOUNT_VARIANCE / TIMING_DIFFERENCE / EXCEPTION),
     a tie is AMBIGUOUS_MATCH, and nothing is ever inferred from PENDING alone.
     (This depends on the documented Stage 5C "[auto-match]"/"[review]" reason
     prefix for the high-value downgrade; Stage 5C itself is unchanged.)
   * A bank transaction in a live (PENDING/ACCEPTED) Stage 5D group is matched.
     Competing AMBIGUOUS groups produce ONE item per anchor transaction.
   * Unmatched ACTIVE bank transactions -> BANK_ONLY. REVERSED rows are never read.
   * Unmatched POSTED ledger transactions in the run scope -> LEDGER_ONLY, unless
     already claimed by a live suggestion or group (any run) or covered by an
     ambiguous group - decided with SQL NOT EXISTS, not by loading the ledger.

2. WORKFLOW - an explicit state machine with row-locked, audited actions. No
   action here ever creates, changes or deletes a TreasuryTransaction,
   BankStatementTransaction, BankBalance or any cash record: resolution means
   "investigated and documented", never "posted".
"""
import datetime
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import BankStatementTransaction, BankStatementTransactionStatus
from app.models.rbac import TreasuryModule, User
from app.models.reconciliation import (
    MatchGroupStatus,
    MatchRelationshipType,
    MatchSuggestionStatus,
    OpenItemCategory,
    OpenItemEventType,
    OpenItemPriority,
    OpenItemStatus,
    ReconciliationConfiguration,
    ReconciliationMatchGroup,
    ReconciliationMatchGroupMember,
    ReconciliationMatchSuggestion,
    ReconciliationOpenItem,
    ReconciliationOpenItemEvent,
    ReconciliationRun,
)
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.services.audit_service import record_audit_event
from app.services.reconciliation_scoring import AUTO_MATCH_THRESHOLD

_MODULE = TreasuryModule.BANK_RECONCILIATION.value
_LIVE_SUGGESTION = (MatchSuggestionStatus.PENDING, MatchSuggestionStatus.ACCEPTED)
_LIVE_GROUP = (MatchGroupStatus.PENDING, MatchGroupStatus.ACCEPTED)
_INSERT_CHUNK = 500

# --------------------------------------------------------------------------------------
# State machine (SECTION 5). Resolution and closure are separate actions; CLOSED and
# REJECTED are terminal for ordinary actions and only an explicit, reasoned REOPEN leaves them.
# --------------------------------------------------------------------------------------
OPEN_ITEM_TRANSITIONS: dict[OpenItemStatus, set[OpenItemStatus]] = {
    OpenItemStatus.OPEN: {OpenItemStatus.UNDER_REVIEW, OpenItemStatus.REJECTED},
    OpenItemStatus.UNDER_REVIEW: {OpenItemStatus.RESOLVED, OpenItemStatus.REJECTED},
    OpenItemStatus.RESOLVED: {OpenItemStatus.CLOSED, OpenItemStatus.OPEN},
    OpenItemStatus.CLOSED: {OpenItemStatus.OPEN},
    OpenItemStatus.REJECTED: {OpenItemStatus.OPEN},
}
ACTIONABLE_STATUSES = (OpenItemStatus.OPEN, OpenItemStatus.UNDER_REVIEW)
MIN_REASON_LENGTH = 10          # resolve / reject / reopen: a meaningful reason, not a placeholder
MIN_ASSIGNMENT_REASON_LENGTH = 3
MAX_NOTE_LENGTH = 4000


# ======================================================================================
# Derivation
# ======================================================================================
@dataclass(frozen=True)
class ItemSpec:
    category: OpenItemCategory
    bank_id: uuid.UUID | None
    treasury_id: uuid.UUID | None
    amount: Decimal
    currency_code: str
    item_date: datetime.date
    description: str
    reason: str
    priority: OpenItemPriority

    @property
    def source_key(self) -> str:
        return f"{self.category.value}:{self.bank_id or '-'}:{self.treasury_id or '-'}"


@dataclass
class SyncResult:
    created: int = 0
    existing: int = 0
    by_category: dict = field(default_factory=dict)

    @property
    def derived(self) -> int:
        return self.created + self.existing


def _priority_for(amount: Decimal, threshold: Decimal | None) -> OpenItemPriority:
    return OpenItemPriority.HIGH if threshold is not None and amount > threshold else OpenItemPriority.MEDIUM


def _is_auto_match_tier(confidence: Decimal | None, reason: str | None) -> bool:
    return confidence is not None and confidence >= AUTO_MATCH_THRESHOLD and not (reason or "").startswith("[review]")


_GROUP_CATEGORY = {
    MatchRelationshipType.ONE_TO_MANY: OpenItemCategory.ONE_TO_MANY,
    MatchRelationshipType.MANY_TO_ONE: OpenItemCategory.MANY_TO_ONE,
    MatchRelationshipType.BATCH: OpenItemCategory.BATCH_PAYMENT,
}


async def derive_open_item_specs(db: AsyncSession, run: ReconciliationRun) -> list[ItemSpec]:
    configuration = await db.get(ReconciliationConfiguration, run.configuration_id) if run.configuration_id else None
    threshold = configuration.high_value_threshold if configuration else None

    bank_rows = (await db.execute(
        select(BankStatementTransaction).where(
            BankStatementTransaction.legal_entity_id == run.legal_entity_id,
            BankStatementTransaction.bank_account_id == run.bank_account_id,
            BankStatementTransaction.transaction_date >= run.period_start,
            BankStatementTransaction.transaction_date <= run.period_end,
            BankStatementTransaction.status == BankStatementTransactionStatus.ACTIVE,  # REVERSED never produces an item
        ).order_by(BankStatementTransaction.transaction_date, BankStatementTransaction.id)
    )).scalars().all()
    bank_by_id = {b.id: b for b in bank_rows}

    suggestions = (await db.execute(
        select(ReconciliationMatchSuggestion).where(ReconciliationMatchSuggestion.reconciliation_run_id == run.id)
    )).scalars().all()
    group_rows = (await db.execute(
        select(
            ReconciliationMatchGroup.id, ReconciliationMatchGroup.status, ReconciliationMatchGroup.relationship_type,
            ReconciliationMatchGroupMember.bank_statement_transaction_id,
            ReconciliationMatchGroupMember.treasury_transaction_id,
        ).join(ReconciliationMatchGroupMember, ReconciliationMatchGroupMember.match_group_id == ReconciliationMatchGroup.id)
        .where(ReconciliationMatchGroup.reconciliation_run_id == run.id)
    )).all()

    real_by_bank: dict = {}
    for s in suggestions:
        if s.bank_statement_transaction_id and s.treasury_transaction_id and s.status in _LIVE_SUGGESTION:
            real_by_bank.setdefault(s.bank_statement_transaction_id, []).append(s)

    review_ledger_ids = {
        s.treasury_transaction_id
        for lst in real_by_bank.values() if len(lst) == 1
        for s in lst if s.status == MatchSuggestionStatus.PENDING and not _is_auto_match_tier(s.confidence, s.reason)
    }
    ledger_by_id = {}
    if review_ledger_ids:
        ledger_by_id = {t.id: t for t in (await db.execute(
            select(TreasuryTransaction).where(TreasuryTransaction.id.in_(review_ledger_ids))
        )).scalars().all()}

    bank_in_live_group: set = set()
    bank_in_ambiguous_group: set = set()
    ambiguous_groups: dict = {}
    for group_id, status, relationship, bank_id, treasury_id in group_rows:
        if status in _LIVE_GROUP and bank_id:
            bank_in_live_group.add(bank_id)
        if status == MatchGroupStatus.AMBIGUOUS:
            entry = ambiguous_groups.setdefault(group_id, {"rel": relationship, "bank": set(), "ledger": set()})
            (entry["bank"] if bank_id else entry["ledger"]).add(bank_id or treasury_id)
            if bank_id:
                bank_in_ambiguous_group.add(bank_id)

    specs: dict = {}

    def add(spec: ItemSpec) -> None:
        specs.setdefault(spec.source_key, spec)

    # --- Ambiguous Stage 5D groups: ONE item per anchor, however many competing groups share it.
    for entry in ambiguous_groups.values():
        category = _GROUP_CATEGORY.get(entry["rel"], OpenItemCategory.EXCEPTION)
        text = ("Competing grouped matches could explain this transaction equally well; "
                "the engine made no automatic selection.")
        if len(entry["bank"]) == 1 and entry["rel"] != MatchRelationshipType.MANY_TO_ONE:
            bank = bank_by_id.get(next(iter(entry["bank"])))
            if bank:
                add(ItemSpec(category, bank.id, None, bank.amount, bank.currency_code, bank.transaction_date, text,
                             "Ambiguous advanced match group", _priority_for(bank.amount, threshold)))
        elif len(entry["ledger"]) == 1:
            ledger = await db.get(TreasuryTransaction, next(iter(entry["ledger"])))
            if ledger:
                add(ItemSpec(category, None, ledger.id, ledger.transaction_amount, ledger.transaction_currency_code,
                             ledger.event_date, text, "Ambiguous advanced match group",
                             _priority_for(ledger.transaction_amount, threshold)))

    # --- Bank side.
    for bank in bank_rows:
        if bank.id in bank_in_live_group or bank.id in bank_in_ambiguous_group:
            continue  # matched by a live group, or already represented by an ambiguous-group item
        candidates = real_by_bank.get(bank.id, [])
        if len(candidates) >= 2:
            add(ItemSpec(OpenItemCategory.AMBIGUOUS_MATCH, bank.id, None, bank.amount, bank.currency_code,
                         bank.transaction_date,
                         f"{len(candidates)} ledger transactions tie as the best one-to-one match; the engine "
                         f"did not pick one.", "Tied top-scoring candidates", _priority_for(bank.amount, threshold)))
            continue
        if len(candidates) == 1:
            s = candidates[0]
            if s.status == MatchSuggestionStatus.ACCEPTED or _is_auto_match_tier(s.confidence, s.reason):
                continue  # a valid match: not an exception
            ledger = ledger_by_id.get(s.treasury_transaction_id)
            if ledger is None:
                continue
            if bank.amount != ledger.transaction_amount:
                variance = abs(bank.amount - ledger.transaction_amount)
                add(ItemSpec(OpenItemCategory.AMOUNT_VARIANCE, bank.id, ledger.id, variance, bank.currency_code, bank.transaction_date,
                             f"Bank {bank.amount} vs ledger {ledger.transaction_amount} {bank.currency_code}: "
                             f"variance {variance}.", s.reason or "Review-tier match", _priority_for(bank.amount, threshold)))
            elif bank.transaction_date != ledger.event_date:
                days = abs((bank.transaction_date - ledger.event_date).days)
                add(ItemSpec(OpenItemCategory.TIMING_DIFFERENCE, bank.id, ledger.id, bank.amount, bank.currency_code,
                             bank.transaction_date, f"Amounts agree but dates differ by {days} day(s).",
                             s.reason or "Review-tier match", _priority_for(bank.amount, threshold)))
            else:
                add(ItemSpec(OpenItemCategory.EXCEPTION, bank.id, ledger.id, bank.amount, bank.currency_code,
                             bank.transaction_date, "Amount and date agree but supporting evidence is weak "
                             "(review-tier match).", s.reason or "Review-tier match", _priority_for(bank.amount, threshold)))
            continue
        add(ItemSpec(OpenItemCategory.BANK_ONLY, bank.id, None, bank.amount, bank.currency_code, bank.transaction_date,
                     "Bank statement transaction with no matching ledger transaction.",
                     "No candidate met the review threshold", _priority_for(bank.amount, threshold)))

    # --- Ledger side: unmatched POSTED ledger rows, decided in SQL (never load the whole ledger).
    live_suggestion = exists().where(
        ReconciliationMatchSuggestion.treasury_transaction_id == TreasuryTransaction.id,
        ReconciliationMatchSuggestion.status.in_(_LIVE_SUGGESTION),
    )
    live_group = exists().where(
        ReconciliationMatchGroupMember.treasury_transaction_id == TreasuryTransaction.id,
        ReconciliationMatchGroupMember.match_group_id == ReconciliationMatchGroup.id,
        ReconciliationMatchGroup.status.in_(_LIVE_GROUP),
    )
    ambiguous_group = exists().where(
        ReconciliationMatchGroupMember.treasury_transaction_id == TreasuryTransaction.id,
        ReconciliationMatchGroupMember.match_group_id == ReconciliationMatchGroup.id,
        ReconciliationMatchGroup.status == MatchGroupStatus.AMBIGUOUS,
        ReconciliationMatchGroup.reconciliation_run_id == run.id,
    )
    ledger_rows = (await db.execute(
        select(TreasuryTransaction).where(
            TreasuryTransaction.legal_entity_id == run.legal_entity_id,
            TreasuryTransaction.bank_account_id == run.bank_account_id,
            TreasuryTransaction.status == TransactionStatus.POSTED,
            TreasuryTransaction.event_date >= run.period_start,
            TreasuryTransaction.event_date <= run.period_end,
            ~live_suggestion, ~live_group, ~ambiguous_group,
        ).order_by(TreasuryTransaction.event_date, TreasuryTransaction.id)
    )).scalars().all()
    for ledger in ledger_rows:
        add(ItemSpec(OpenItemCategory.LEDGER_ONLY, None, ledger.id, ledger.transaction_amount,
                     ledger.transaction_currency_code, ledger.event_date,
                     "Ledger transaction with no matching bank statement transaction.",
                     "Not claimed by any live match", _priority_for(ledger.transaction_amount, threshold)))

    return list(specs.values())


async def _persist_specs(
    db: AsyncSession, run: ReconciliationRun, specs: list[ItemSpec], actor_user_id: uuid.UUID | None,
) -> SyncResult:
    result = SyncResult()
    for start in range(0, len(specs), _INSERT_CHUNK):
        chunk = specs[start:start + _INSERT_CHUNK]
        rows = [{
            "id": uuid.uuid4(), "reconciliation_run_id": run.id, "legal_entity_id": run.legal_entity_id,
            "bank_account_id": run.bank_account_id, "bank_statement_transaction_id": s.bank_id,
            "treasury_transaction_id": s.treasury_id, "category": s.category, "status": OpenItemStatus.OPEN,
            "amount": s.amount, "currency_code": s.currency_code, "description": s.description, "reason": s.reason,
            "source_key": s.source_key, "item_date": s.item_date, "priority": s.priority,
        } for s in chunk]
        inserted = (await db.execute(
            pg_insert(ReconciliationOpenItem).values(rows)
            .on_conflict_do_nothing(constraint="uq_reconciliation_open_item_source")
            .returning(ReconciliationOpenItem.id, ReconciliationOpenItem.category)
        )).all()
        result.created += len(inserted)
        result.existing += len(chunk) - len(inserted)
        for _, category in inserted:
            result.by_category[category.value] = result.by_category.get(category.value, 0) + 1
        if inserted:
            await db.execute(pg_insert(ReconciliationOpenItemEvent).values([{
                "id": uuid.uuid4(), "open_item_id": item_id, "legal_entity_id": run.legal_entity_id,
                "event_type": OpenItemEventType.CREATED, "actor_user_id": actor_user_id,
                "from_status": None, "to_status": OpenItemStatus.OPEN,
                "note": "Created by reconciliation run synchronization.",
            } for item_id, _ in inserted]))
    return result


async def sync_open_items_for_run(
    db: AsyncSession, run: ReconciliationRun, actor_user_id: uuid.UUID | None = None,
) -> SyncResult:
    """
    Idempotent and atomic: everything happens inside a SAVEPOINT, so a failure
    leaves no partial items/events, and the (run, source_key) unique constraint
    plus ON CONFLICT DO NOTHING means re-running creates nothing new - no
    duplicate items, no duplicate CREATED events, and an audit event only when
    something was actually created.
    """
    async with db.begin_nested():
        specs = await derive_open_item_specs(db, run)
        result = await _persist_specs(db, run, specs, actor_user_id)
        if result.created:
            await record_audit_event(
                db, module=_MODULE, action="SYNC_OPEN_ITEMS", record_type="ReconciliationRun",
                record_id=str(run.id), user_id=actor_user_id, legal_entity_id=run.legal_entity_id,
                new_value={"created": result.created, "already_existing": result.existing,
                           "by_category": result.by_category},
            )
    return result


# ======================================================================================
# Workflow
# ======================================================================================
async def load_item_for_update(db: AsyncSession, item_id: uuid.UUID) -> ReconciliationOpenItem | None:
    """Row-locks the item so two conflicting actions serialize instead of racing."""
    return (await db.execute(
        select(ReconciliationOpenItem).where(ReconciliationOpenItem.id == item_id).with_for_update()
    )).scalar_one_or_none()


def _clean_text(value: str | None, *, minimum: int, label: str) -> str:
    text_value = (value or "").strip()
    if len(text_value) < minimum:
        raise HTTPException(status_code=422, detail=f"{label} is required (at least {minimum} characters).")
    if len(text_value) > MAX_NOTE_LENGTH:
        raise HTTPException(status_code=422, detail=f"{label} is too long (maximum {MAX_NOTE_LENGTH} characters).")
    return text_value


def _require_transition(item: ReconciliationOpenItem, target: OpenItemStatus) -> None:
    allowed = OPEN_ITEM_TRANSITIONS.get(item.status, set())
    if target not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot move open item from {item.status.value} to {target.value}. "
                   f"Allowed: {sorted(s.value for s in allowed) or 'none'}.",
        )


def _snapshot(item: ReconciliationOpenItem) -> dict:
    return {"status": item.status.value, "assigned_to_user_id": str(item.assigned_to_user_id)
            if item.assigned_to_user_id else None, "priority": item.priority.value,
            "due_date": item.due_date.isoformat() if item.due_date else None}


async def _record(
    db: AsyncSession, item: ReconciliationOpenItem, *, event_type: OpenItemEventType, actor: User,
    audit_action: str, previous: dict | None, from_status: OpenItemStatus | None, to_status: OpenItemStatus | None,
    note: str | None = None, reference: str | None = None, assignee_user_id: uuid.UUID | None = None,
) -> ReconciliationOpenItemEvent:
    event = ReconciliationOpenItemEvent(
        open_item_id=item.id, legal_entity_id=item.legal_entity_id, event_type=event_type, actor_user_id=actor.id,
        from_status=from_status, to_status=to_status, assignee_user_id=assignee_user_id, note=note, reference=reference,
    )
    db.add(event)
    await db.flush()
    await record_audit_event(
        db, module=_MODULE, action=audit_action, record_type="ReconciliationOpenItem", record_id=str(item.id),
        user_id=actor.id, legal_entity_id=item.legal_entity_id, previous_value=previous, new_value=_snapshot(item),
        reason=note if note else reference,
    )
    return event


async def start_investigation(db, item, actor: User):
    _require_transition(item, OpenItemStatus.UNDER_REVIEW)
    previous, before = _snapshot(item), item.status
    item.status = OpenItemStatus.UNDER_REVIEW
    return await _record(db, item, event_type=OpenItemEventType.INVESTIGATION_STARTED, actor=actor,
                         audit_action="INVESTIGATION_STARTED", previous=previous, from_status=before,
                         to_status=item.status)


async def assign_item(db, item, actor: User, assignee: User, reason: str, due_date=None, priority=None):
    if item.status not in ACTIONABLE_STATUSES:
        raise HTTPException(status_code=400, detail=f"Cannot assign an item in status {item.status.value}; "
                                                     f"reopen it first.")
    if item.assigned_to_user_id == assignee.id:
        raise HTTPException(status_code=400, detail="Item is already assigned to this user.")
    reason = _clean_text(reason, minimum=MIN_ASSIGNMENT_REASON_LENGTH, label="Assignment reason")
    previous = _snapshot(item)
    event_type = OpenItemEventType.REASSIGNED if item.assigned_to_user_id else OpenItemEventType.ASSIGNED
    item.assigned_to_user_id = assignee.id
    item.assigned_by_user_id = actor.id
    item.assigned_at = datetime.datetime.now(datetime.UTC)
    if due_date is not None:
        item.due_date = due_date
    if priority is not None:
        item.priority = priority
    return await _record(db, item, event_type=event_type, actor=actor, audit_action=event_type.value,
                         previous=previous, from_status=item.status, to_status=item.status, note=reason,
                         assignee_user_id=assignee.id)


async def add_comment(db, item, actor: User, note: str):
    note = _clean_text(note, minimum=1, label="Comment")
    return await _record(db, item, event_type=OpenItemEventType.COMMENT, actor=actor, audit_action="COMMENT_ADDED",
                         previous=None, from_status=item.status, to_status=item.status, note=note)


def validate_evidence_reference(reference: str | None) -> str:
    """Evidence is a plain-text reference (document / journal / ticket number) - never a URL or a
    filesystem path, because the repository has no secure evidence-file store."""
    value = (reference or "").strip()
    if not value or len(value) > 255:
        raise HTTPException(status_code=422, detail="Evidence reference is required (maximum 255 characters).")
    lowered = value.lower()
    looks_like_path = (
        "\\" in value or value.startswith(("/", "~")) or ".." in value
        or (len(value) >= 2 and value[1] == ":" and value[0].isalpha())  # Windows drive path, e.g. C:...
    )
    if "://" in value or looks_like_path or lowered.startswith(("file:", "data:")):
        raise HTTPException(status_code=422, detail="Evidence must be a document reference (e.g. a journal or "
                                                     "ticket number), not a URL or file path.")
    return value


async def add_evidence(db, item, actor: User, reference: str, note: str | None):
    reference = validate_evidence_reference(reference)
    note = _clean_text(note, minimum=0, label="Note") if note else None
    return await _record(db, item, event_type=OpenItemEventType.EVIDENCE, actor=actor, audit_action="EVIDENCE_ADDED",
                         previous=None, from_status=item.status, to_status=item.status, note=note, reference=reference)


async def resolve_item(db, item, actor: User, reason: str):
    _require_transition(item, OpenItemStatus.RESOLVED)
    reason = _clean_text(reason, minimum=MIN_REASON_LENGTH, label="Resolution reason")
    previous, before = _snapshot(item), item.status
    item.status = OpenItemStatus.RESOLVED
    item.resolved_at = datetime.datetime.now(datetime.UTC)
    item.resolved_by_user_id = actor.id
    item.resolution_reason = reason
    return await _record(db, item, event_type=OpenItemEventType.RESOLVED, actor=actor, audit_action="RESOLVED",
                         previous=previous, from_status=before, to_status=item.status, note=reason)


async def close_item(db, item, actor: User, note: str | None):
    _require_transition(item, OpenItemStatus.CLOSED)
    note = _clean_text(note, minimum=0, label="Closure note") if note else None
    previous, before = _snapshot(item), item.status
    item.status = OpenItemStatus.CLOSED
    item.closed_at = datetime.datetime.now(datetime.UTC)
    item.closed_by_user_id = actor.id
    return await _record(db, item, event_type=OpenItemEventType.CLOSED, actor=actor, audit_action="CLOSED",
                         previous=previous, from_status=before, to_status=item.status, note=note)


async def reject_item(db, item, actor: User, reason: str):
    _require_transition(item, OpenItemStatus.REJECTED)
    reason = _clean_text(reason, minimum=MIN_REASON_LENGTH, label="Rejection reason")
    previous, before = _snapshot(item), item.status
    item.status = OpenItemStatus.REJECTED
    item.rejected_at = datetime.datetime.now(datetime.UTC)
    item.rejection_reason = reason
    return await _record(db, item, event_type=OpenItemEventType.REJECTED, actor=actor, audit_action="REJECTED",
                         previous=previous, from_status=before, to_status=item.status, note=reason)


async def reopen_item(db, item, actor: User, reason: str):
    _require_transition(item, OpenItemStatus.OPEN)
    reason = _clean_text(reason, minimum=MIN_REASON_LENGTH, label="Reopen reason")
    previous, before = _snapshot(item), item.status
    # The summary fields describe the CURRENT disposition, so they are cleared; the full history
    # of the earlier resolution/closure/rejection stays in the append-only event table.
    item.status = OpenItemStatus.OPEN
    item.resolved_at = item.resolved_by_user_id = item.resolution_reason = None
    item.closed_at = item.closed_by_user_id = None
    item.rejected_at = item.rejection_reason = None
    return await _record(db, item, event_type=OpenItemEventType.REOPENED, actor=actor, audit_action="REOPENED",
                         previous=previous, from_status=before, to_status=item.status, note=reason)
