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
"""
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import TreasuryModule, User
from app.models.reconciliation import (
    MatchDecisionType,
    MatchGroupStatus,
    MatchProposalKind,
    MatchSuggestionStatus,
    ReconciliationMatchDecision,
    ReconciliationMatchGroup,
    ReconciliationMatchSuggestion,
    ReconciliationRun,
    ReconciliationRunStatus,
)
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

    proposal.status = (MatchSuggestionStatus if kind == MatchProposalKind.SUGGESTION else MatchGroupStatus)[target_status]
    record = ReconciliationMatchDecision(
        reconciliation_run_id=run.id, legal_entity_id=run.legal_entity_id, kind=kind,
        suggestion_id=proposal.id if kind == MatchProposalKind.SUGGESTION else None,
        group_id=proposal.id if kind == MatchProposalKind.GROUP else None,
        decision=decision, previous_status=current_status, new_status=target_status,
        reason=text or None, decided_by_user_id=actor.id,
    )
    db.add(record)
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
