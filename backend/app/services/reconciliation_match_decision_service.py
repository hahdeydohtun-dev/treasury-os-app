"""
Stage 5E: explicit accept/reject decisions on engine-proposed matches.

Disposition semantics (documented in docs/STAGE_5E_OPEN_ITEM_WORKFLOW.md):

  PENDING   a proposal from Stage 5C (one-to-one suggestion) or Stage 5D (match group) awaiting a decision.
            It is NOT a reconciliation, however high its confidence - the engines never accept anything.
  ACCEPTED  set ONLY by `decide_proposal`, by an authorized user, with a decision record.
  REJECTED  set ONLY by `decide_proposal`, with a mandatory reason; the covered transactions become
            unmatched again and `sync_open_items_for_run` turns them into actionable exceptions.

What is decidable: an UNAMBIGUOUS auto-tier one-to-one suggestion and any PENDING Stage 5D group of any
relationship type (ONE_TO_MANY, MANY_TO_ONE, BATCH, INTERNAL_TRANSFER, FX_MATCH). Everything else is
deliberately NOT decidable here because it is already an investigation item: tied candidates and AMBIGUOUS
groups (a human must first work out which is right) and review-tier suggestions (weak evidence). A decision
changes only the proposal's status and writes an append-only record - it never touches a
TreasuryTransaction, BankStatementTransaction or BankBalance, and posts nothing.

Duplicate-acceptance protection (see `find_acceptance_conflict` and the `reconciliation_accepted_claims` table):
a bank-statement or ledger transaction can be part of at most ONE accepted proposal per namespace
(MATCH for bank-to-ledger proposals, TRANSFER for INTERNAL_TRANSFER groups), across ALL runs. Acceptance first
checks eligibility and conflicts for a clear 409, and the claim INSERT is the real guarantee: partial unique
indexes make a conflicting acceptance impossible even when two requests race past the pre-check, because the
second INSERT waits for the first transaction and then fails (mapped to 409, with nothing else written).
Only ACCEPTED proposals claim: two overlapping PENDING proposals may coexist, and accepting one never
accepts or rejects the other.
"""
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import BankStatementTransaction, BankStatementTransactionStatus
from app.models.rbac import TreasuryModule, User
from app.models.reconciliation import (
    AcceptedClaimKind,
    MatchDecisionType,
    MatchGroupStatus,
    MatchProposalKind,
    MatchRelationshipType,
    MatchSuggestionStatus,
    ReconciliationAcceptedClaim,
    ReconciliationMatchDecision,
    ReconciliationMatchGroup,
    ReconciliationMatchGroupMember,
    ReconciliationMatchSuggestion,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.services.audit_service import record_audit_event
from app.services.reconciliation_open_item_service import (
    _is_auto_match_tier,
    sync_open_items_for_run,
)

_MODULE = TreasuryModule.BANK_RECONCILIATION.value
MIN_REJECTION_REASON_LENGTH = 10
_LIVE_SUGGESTION = (MatchSuggestionStatus.PENDING, MatchSuggestionStatus.ACCEPTED)


@dataclass
class DecisionResult:
    decision: ReconciliationMatchDecision
    already_decided: bool
    run: ReconciliationRun


async def load_proposal(db: AsyncSession, kind: MatchProposalKind, proposal_id: uuid.UUID, *, lock: bool = False):
    model = ReconciliationMatchSuggestion if kind == MatchProposalKind.SUGGESTION else ReconciliationMatchGroup
    stmt = select(model).where(model.id == proposal_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def has_other_live_candidate(db: AsyncSession, s: ReconciliationMatchSuggestion) -> bool:
    """True if the same bank transaction has another live one-to-one candidate in the same run (a tie)."""
    other = (await db.execute(
        select(ReconciliationMatchSuggestion.id).where(
            ReconciliationMatchSuggestion.reconciliation_run_id == s.reconciliation_run_id,
            ReconciliationMatchSuggestion.bank_statement_transaction_id == s.bank_statement_transaction_id,
            ReconciliationMatchSuggestion.treasury_transaction_id.is_not(None),
            ReconciliationMatchSuggestion.status.in_(_LIVE_SUGGESTION),
            ReconciliationMatchSuggestion.id != s.id,
        ).limit(1)
    )).first()
    return other is not None


async def suggestion_not_decidable_reason(db: AsyncSession, s: ReconciliationMatchSuggestion) -> str | None:
    if s.treasury_transaction_id is None or s.bank_statement_transaction_id is None:
        return "This is a 'no candidate found' placeholder, not a proposed match."
    if await has_other_live_candidate(db, s):
        return ("Several candidates tie for this bank transaction. It is an ambiguous-match exception: "
                "investigate it from the open-items queue.")
    if not _is_auto_match_tier(s.confidence, s.reason):
        return ("This is a review-tier (weak-evidence) match, which is an exception to investigate from the "
                "open-items queue rather than a proposal to approve.")
    return None


@dataclass(frozen=True)
class ClaimSpec:
    kind: AcceptedClaimKind
    bank_id: uuid.UUID | None
    treasury_id: uuid.UUID | None


async def proposal_claims(db: AsyncSession, kind: MatchProposalKind, proposal) -> list[ClaimSpec]:
    """Every transaction an accepted `proposal` would claim, with its namespace."""
    if kind == MatchProposalKind.SUGGESTION:
        return [ClaimSpec(AcceptedClaimKind.MATCH, proposal.bank_statement_transaction_id, None),
                ClaimSpec(AcceptedClaimKind.MATCH, None, proposal.treasury_transaction_id)]
    namespace = (AcceptedClaimKind.TRANSFER if proposal.relationship_type == MatchRelationshipType.INTERNAL_TRANSFER
                 else AcceptedClaimKind.MATCH)
    members = (await db.execute(
        select(ReconciliationMatchGroupMember).where(ReconciliationMatchGroupMember.match_group_id == proposal.id)
    )).scalars().all()
    return [ClaimSpec(namespace, m.bank_statement_transaction_id, m.treasury_transaction_id) for m in members]


async def eligibility_problem(
    db: AsyncSession, kind: MatchProposalKind, proposal, run: ReconciliationRun, claims: list[ClaimSpec],
) -> str | None:
    """
    Every underlying transaction must still be eligible: a bank row must be ACTIVE (not REVERSED) and a ledger
    row POSTED, both in the run's legal entity and bank account (an INTERNAL_TRANSFER pair leg may sit in another
    account of the same entity), with currencies consistent with the proposal. This guards against data that
    changed after the engine produced the proposal.
    """
    bank_ids = [c.bank_id for c in claims if c.bank_id]
    ledger_ids = [c.treasury_id for c in claims if c.treasury_id]
    banks = {b.id: b for b in (await db.execute(
        select(BankStatementTransaction).where(BankStatementTransaction.id.in_(bank_ids)))).scalars()} if bank_ids else {}
    ledgers = {t.id: t for t in (await db.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.id.in_(ledger_ids)))).scalars()} if ledger_ids else {}
    transfer = kind == MatchProposalKind.GROUP and proposal.relationship_type == MatchRelationshipType.INTERNAL_TRANSFER
    fx = kind == MatchProposalKind.GROUP and proposal.relationship_type == MatchRelationshipType.FX_MATCH

    for bank_id in bank_ids:
        b = banks.get(bank_id)
        if b is None or b.status != BankStatementTransactionStatus.ACTIVE:
            return f"Bank statement transaction {bank_id} is no longer eligible (missing or reversed)."
        if b.legal_entity_id != run.legal_entity_id or b.bank_account_id != run.bank_account_id:
            return f"Bank statement transaction {bank_id} is outside this run's legal entity / bank account."
    for ledger_id in ledger_ids:
        t = ledgers.get(ledger_id)
        if t is None or t.status != TransactionStatus.POSTED:
            return f"Ledger transaction {ledger_id} is no longer eligible (missing or not POSTED)."
        if t.legal_entity_id != run.legal_entity_id or (not transfer and t.bank_account_id != run.bank_account_id):
            return f"Ledger transaction {ledger_id} is outside this run's legal entity / bank account."

    if kind == MatchProposalKind.SUGGESTION:
        if banks[bank_ids[0]].currency_code != ledgers[ledger_ids[0]].transaction_currency_code:
            return "The bank and ledger transactions are in different currencies."
    elif fx:
        if any(b.currency_code != proposal.fx_source_currency_code for b in banks.values()) or any(
                t.transaction_currency_code != proposal.fx_target_currency_code for t in ledgers.values()):
            return "A member's currency no longer matches the FX proposal's source/target currencies."
    elif not transfer and (
        any(b.currency_code != proposal.currency_code for b in banks.values())
        or any(t.transaction_currency_code != proposal.currency_code for t in ledgers.values())
    ):
        return "A member's currency does not match the proposal's currency."
    return None


async def find_acceptance_conflict(db: AsyncSession, claims: list[ClaimSpec]) -> str | None:
    """A clear message if any claimed transaction is already part of an ACCEPTED proposal (any run), else None."""
    conditions = [
        and_(ReconciliationAcceptedClaim.claim_kind == c.kind,
             ReconciliationAcceptedClaim.bank_statement_transaction_id == c.bank_id) if c.bank_id
        else and_(ReconciliationAcceptedClaim.claim_kind == c.kind,
                  ReconciliationAcceptedClaim.treasury_transaction_id == c.treasury_id)
        for c in claims
    ]
    if not conditions:
        return None
    row = (await db.execute(
        select(ReconciliationAcceptedClaim, ReconciliationMatchDecision)
        .join(ReconciliationMatchDecision, ReconciliationMatchDecision.id == ReconciliationAcceptedClaim.decision_id)
        .where(or_(*conditions)).limit(1)
    )).first()
    if row is None:
        return None
    claim, prior = row
    what = (f"bank statement transaction {claim.bank_statement_transaction_id}" if claim.bank_statement_transaction_id
            else f"ledger transaction {claim.treasury_transaction_id}")
    return (f"Conflicts with an accepted proposal: {what} is already part of an accepted "
            f"{prior.kind.value.lower()} (decision {prior.id}, run {prior.reconciliation_run_id}). "
            f"A transaction cannot be accepted through two proposals.")


async def acceptance_blocker(db: AsyncSession, kind: MatchProposalKind, proposal, run: ReconciliationRun) -> str | None:
    """Why this PENDING proposal could not be accepted right now (ineligible member or conflicting acceptance)."""
    claims = await proposal_claims(db, kind, proposal)
    return await eligibility_problem(db, kind, proposal, run, claims) or await find_acceptance_conflict(db, claims)


async def decide_proposal(
    db: AsyncSession, *, kind: MatchProposalKind, proposal_id: uuid.UUID, actor: User,
    decision: MatchDecisionType, reason: str | None,
) -> DecisionResult:
    """
    Authorization and entity scoping are the CALLER's job (done before this is invoked). Here: lock the
    proposal row, then
      * same decision already recorded      -> idempotent no-op (no new record, no audit event);
      * a different decision already exists -> 409, nothing changes;
      * PENDING and decidable               -> apply, record, audit (and, on rejection, re-derive exceptions).
    """
    proposal = await load_proposal(db, kind, proposal_id, lock=True)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Match proposal not found.")
    run = await db.get(ReconciliationRun, proposal.reconciliation_run_id)
    if run is None:  # cannot happen (foreign key) but keeps the type honest and fails closed
        raise HTTPException(status_code=404, detail="Match proposal not found.")
    target_status = decision.value  # "ACCEPTED" / "REJECTED" - names match the existing enums

    existing = (await db.execute(
        select(ReconciliationMatchDecision).where(
            (ReconciliationMatchDecision.suggestion_id == proposal.id) if kind == MatchProposalKind.SUGGESTION
            else (ReconciliationMatchDecision.group_id == proposal.id)
        )
    )).scalar_one_or_none()
    if existing is not None:
        if existing.decision == decision:
            return DecisionResult(existing, True, run)
        raise HTTPException(status_code=409, detail=f"This proposal was already {existing.decision.value.lower()}; "
                                                    f"decisions are final and cannot be reversed here.")

    current_status = proposal.status.value
    if run.status != ReconciliationRunStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Decisions can only be made on proposals of a COMPLETED run.")
    if current_status != "PENDING":
        hint = (" Competing ambiguous groups are exceptions to investigate from the open-items queue."
                if current_status == "AMBIGUOUS" else "")
        raise HTTPException(status_code=400, detail=f"Only a PENDING proposal can be decided "
                                                    f"(this one is {current_status}).{hint}")
    if kind == MatchProposalKind.SUGGESTION:
        blocker = await suggestion_not_decidable_reason(db, proposal)
        if blocker:
            raise HTTPException(status_code=400, detail=blocker)

    text = (reason or "").strip()
    if decision == MatchDecisionType.REJECTED and len(text) < MIN_REJECTION_REASON_LENGTH:
        raise HTTPException(status_code=422, detail=f"A rejection reason is required (at least "
                                                    f"{MIN_REJECTION_REASON_LENGTH} characters).")
    if len(text) > 4000:
        raise HTTPException(status_code=422, detail="Reason is too long (maximum 4000 characters).")

    record = ReconciliationMatchDecision(
        reconciliation_run_id=run.id, legal_entity_id=run.legal_entity_id, kind=kind,
        suggestion_id=proposal.id if kind == MatchProposalKind.SUGGESTION else None,
        group_id=proposal.id if kind == MatchProposalKind.GROUP else None,
        decision=decision, previous_status=current_status, new_status=target_status,
        reason=text or None, decided_by_user_id=actor.id,
    )
    if decision == MatchDecisionType.ACCEPTED:
        claims = await proposal_claims(db, kind, proposal)
        problem = await eligibility_problem(db, kind, proposal, run, claims)
        if problem:
            raise HTTPException(status_code=409, detail=f"Cannot accept: {problem}")
        conflict = await find_acceptance_conflict(db, claims)
        if conflict:
            raise HTTPException(status_code=409, detail=conflict)
        # The real guarantee. The decision and its claims are written inside a savepoint BEFORE anything else
        # changes. If another transaction accepted an overlapping proposal after our pre-check, our INSERT waits
        # for that transaction and then violates a unique index; we then write NOTHING (no decision, no status
        # change, no audit event) and report 409.
        try:
            async with db.begin_nested():
                db.add(record)
                await db.flush()
                db.add_all([
                    ReconciliationAcceptedClaim(
                        decision_id=record.id, legal_entity_id=run.legal_entity_id, claim_kind=c.kind,
                        bank_statement_transaction_id=c.bank_id, treasury_transaction_id=c.treasury_id)
                    for c in claims
                ])
                await db.flush()
        except IntegrityError as exc:
            if "ux_accepted_claim" not in str(exc.orig):
                raise
            raise HTTPException(
                status_code=409,
                detail=(await find_acceptance_conflict(db, claims))
                or "Conflicts with a proposal accepted concurrently. A transaction cannot be accepted twice.",
            ) from None
    else:
        db.add(record)
        await db.flush()
    proposal.status = (MatchSuggestionStatus if kind == MatchProposalKind.SUGGESTION else MatchGroupStatus)[target_status]
    await db.flush()
    await record_audit_event(
        db, module=_MODULE, action=f"MATCH_{target_status}",
        record_type="ReconciliationMatchSuggestion" if kind == MatchProposalKind.SUGGESTION else "ReconciliationMatchGroup",
        record_id=str(proposal.id), user_id=actor.id, legal_entity_id=run.legal_entity_id,
        previous_value={"status": current_status, "reconciliation_run_id": str(run.id)},
        new_value={"status": target_status, "reconciliation_run_id": str(run.id), "decision_id": str(record.id)},
        reason=text or None,
    )
    if decision == MatchDecisionType.REJECTED:
        # The rejected proposal no longer covers its transactions: surface them as actionable exceptions
        # now, in the same transaction. Existing items (incl. resolved/closed) are never touched.
        await sync_open_items_for_run(db, run, actor.id)
    return DecisionResult(record, False, run)
