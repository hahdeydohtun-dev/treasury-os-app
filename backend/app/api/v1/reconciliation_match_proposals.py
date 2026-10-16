"""
Stage 5E: match-review queue - engine PROPOSALS awaiting an explicit decision.

A proposal is either a Stage 5C one-to-one suggestion or a Stage 5D match group. PENDING means "proposed,
not reconciled". Accepting or rejecting is an explicit, audited decision (permission: BANK_RECONCILIATION
`APPROVE`, the repository's existing decision permission, also used for funding/facility/investment
approvals); viewing needs `VIEW`. A proposal the caller cannot view is reported as 404, never 403.
Deciding never touches a ledger, bank-statement or cash record.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.auth.authorization import (
    apply_resolved_entity_scope,
    assert_entity_access,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.bank_statement import BankStatementTransaction
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.models.reconciliation import (
    MatchDecisionType,
    MatchGroupStatus,
    MatchProposalKind,
    MatchRelationshipType,
    MatchSuggestionStatus,
    OpenItemStatus,
    ReconciliationMatchDecision,
    ReconciliationMatchGroup,
    ReconciliationMatchGroupMember,
    ReconciliationMatchSuggestion,
    ReconciliationOpenItem,
    ReconciliationRun,
)
from app.models.treasury_transaction import TreasuryTransaction
from app.services import reconciliation_match_decision_service as decisions
from app.services.reconciliation_scoring import AUTO_MATCH_THRESHOLD

router = APIRouter(prefix="/reconciliation/match-proposals", tags=["reconciliation-match-proposals"])
MODULE = TreasuryModule.BANK_RECONCILIATION
S, G, R = ReconciliationMatchSuggestion, ReconciliationMatchGroup, ReconciliationRun
_LIVE = (MatchSuggestionStatus.PENDING, MatchSuggestionStatus.ACCEPTED)
DECIDED = ("ACCEPTED", "REJECTED")


class DecisionRequest(BaseModel):
    reason: str | None = None


async def _can_view(db: AsyncSession, user: User, entity_id: uuid.UUID) -> bool:
    try:
        await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, entity_id)
        return True
    except HTTPException:
        return False


async def _scoped_entity_ids(db: AsyncSession, user: User, legal_entity_id: uuid.UUID | None):
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No BANK_RECONCILIATION:VIEW permission.")
    if legal_entity_id is not None and not await _can_view(db, user, legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")
    return await resolve_scope_entity_ids(db, scope)


def _suggestion_pending_decidable():
    """SQL mirror of decisions.suggestion_not_decidable_reason() == None for a PENDING suggestion:
    a real candidate, auto-tier, and no tie with another live candidate of the same bank transaction."""
    other = aliased(S)
    tie = select(other.id).where(
        other.reconciliation_run_id == S.reconciliation_run_id,
        other.bank_statement_transaction_id == S.bank_statement_transaction_id,
        other.treasury_transaction_id.is_not(None), other.status.in_(_LIVE), other.id != S.id,
    ).exists()
    auto = and_(S.confidence >= AUTO_MATCH_THRESHOLD, or_(S.reason.is_(None), ~S.reason.startswith("[review]")))
    return and_(S.status == MatchSuggestionStatus.PENDING, S.treasury_transaction_id.is_not(None),
                S.bank_statement_transaction_id.is_not(None), auto, ~tie)


def _status_filter_suggestions(statuses: list[str]):
    clauses = []
    if "PENDING" in statuses:
        clauses.append(_suggestion_pending_decidable())
    decided = [MatchSuggestionStatus[x] for x in statuses if x in DECIDED]
    if decided:
        clauses.append(S.status.in_(decided))
    return or_(*clauses) if clauses else S.id.is_(None)


def _status_filter_groups(statuses: list[str]):
    # statuses were validated as PENDING / ACCEPTED / REJECTED by the endpoint (AMBIGUOUS is excluded separately).
    return G.status.in_([MatchGroupStatus[x] for x in statuses])


def _suggestion_stmt(resolved_ids, statuses, run_id, legal_entity_id):
    stmt = (select(S, R.legal_entity_id, R.bank_account_id, BankStatementTransaction, TreasuryTransaction)
            .join(R, R.id == S.reconciliation_run_id)
            .join(BankStatementTransaction, BankStatementTransaction.id == S.bank_statement_transaction_id)
            .join(TreasuryTransaction, TreasuryTransaction.id == S.treasury_transaction_id)
            .where(_status_filter_suggestions(statuses)))
    stmt = apply_resolved_entity_scope(stmt, R.legal_entity_id, resolved_ids)
    if run_id:
        stmt = stmt.where(S.reconciliation_run_id == run_id)
    if legal_entity_id:
        stmt = stmt.where(R.legal_entity_id == legal_entity_id)
    return stmt


def _group_stmt(resolved_ids, statuses, run_id, legal_entity_id, relationship):
    # AMBIGUOUS groups are never listed here: they are exceptions to investigate, not proposals to decide.
    stmt = (select(G, R.legal_entity_id, R.bank_account_id).join(R, R.id == G.reconciliation_run_id)
            .where(_status_filter_groups(statuses), G.status != MatchGroupStatus.AMBIGUOUS))
    stmt = apply_resolved_entity_scope(stmt, R.legal_entity_id, resolved_ids)
    if run_id:
        stmt = stmt.where(G.reconciliation_run_id == run_id)
    if legal_entity_id:
        stmt = stmt.where(R.legal_entity_id == legal_entity_id)
    if relationship:
        stmt = stmt.where(G.relationship_type == relationship)
    return stmt


def _decision_dict(d: ReconciliationMatchDecision | None) -> dict | None:
    if d is None:
        return None
    return {"id": str(d.id), "decision": d.decision.value, "reason": d.reason,
            "decided_by_user_id": str(d.decided_by_user_id), "decided_at": d.created_at.isoformat(),
            "previous_status": d.previous_status, "new_status": d.new_status}


async def _decisions_for(db: AsyncSession, suggestion_ids: list, group_ids: list) -> dict:
    found: dict = {}
    if suggestion_ids:
        for d in (await db.execute(select(ReconciliationMatchDecision).where(
                ReconciliationMatchDecision.suggestion_id.in_(suggestion_ids)))).scalars():
            found[("SUGGESTION", d.suggestion_id)] = d
    if group_ids:
        for d in (await db.execute(select(ReconciliationMatchDecision).where(
                ReconciliationMatchDecision.group_id.in_(group_ids)))).scalars():
            found[("GROUP", d.group_id)] = d
    return found


@router.get("")
async def list_match_proposals(
    status: list[str] | None = Query(None, description="PENDING (default), ACCEPTED, REJECTED"),
    kind: MatchProposalKind | None = None,
    relationship_type: MatchRelationshipType | None = None,
    run_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    statuses = [x.upper() for x in (status or ["PENDING"])]
    if any(x not in ("PENDING", *DECIDED) for x in statuses):
        raise HTTPException(status_code=422, detail="status must be PENDING, ACCEPTED or REJECTED.")
    resolved_ids = await _scoped_entity_ids(db, user, legal_entity_id)
    rows: list[dict] = []

    if kind in (None, MatchProposalKind.SUGGESTION) and relationship_type is None:
        for s, entity_id, account_id, bank, ledger in (await db.execute(
                _suggestion_stmt(resolved_ids, statuses, run_id, legal_entity_id))).all():
            rows.append({
                "kind": "SUGGESTION", "id": str(s.id), "reconciliation_run_id": str(s.reconciliation_run_id),
                "legal_entity_id": str(entity_id), "bank_account_id": str(account_id), "status": s.status.value,
                "label": "ONE_TO_ONE", "match_type": s.match_type.value if s.match_type else None,
                "confidence": str(s.confidence) if s.confidence is not None else None,
                "amount": str(bank.amount), "currency_code": bank.currency_code,
                "date": bank.transaction_date.isoformat(), "bank_reference": bank.bank_reference,
                "ledger_reference": ledger.reference, "member_count": 2, "reason": s.reason,
                "created_at": s.created_at.isoformat(), "decision": None,
            })
    if kind in (None, MatchProposalKind.GROUP):
        group_rows = (await db.execute(_group_stmt(resolved_ids, statuses, run_id, legal_entity_id,
                                                   relationship_type))).all()
        counts: dict[uuid.UUID, int] = {}
        if group_rows:
            member_counts = (await db.execute(
                select(ReconciliationMatchGroupMember.match_group_id, func.count())
                .where(ReconciliationMatchGroupMember.match_group_id.in_([g.id for g, _, _ in group_rows]))
                .group_by(ReconciliationMatchGroupMember.match_group_id))).all()
            counts = {group_id: int(n) for group_id, n in member_counts}
        for g, entity_id, account_id in group_rows:
            rows.append({
                "kind": "GROUP", "id": str(g.id), "reconciliation_run_id": str(g.reconciliation_run_id),
                "legal_entity_id": str(entity_id), "bank_account_id": str(account_id), "status": g.status.value,
                "label": g.relationship_type.value, "match_type": None, "confidence": str(g.confidence),
                "amount": str(g.bank_aggregate_amount if g.bank_aggregate_amount else g.ledger_aggregate_amount),
                "currency_code": g.currency_code, "date": None, "bank_reference": None, "ledger_reference": None,
                "member_count": counts.get(g.id, 0), "reason": g.reason, "created_at": g.created_at.isoformat(),
                "decision": None,
            })
    found = await _decisions_for(
        db, [uuid.UUID(r["id"]) for r in rows if r["kind"] == "SUGGESTION"],
        [uuid.UUID(r["id"]) for r in rows if r["kind"] == "GROUP"],
    )
    for r in rows:
        r["decision"] = _decision_dict(found.get((r["kind"], uuid.UUID(r["id"]))))
    rows.sort(key=lambda r: (r["created_at"], r["id"]))
    return rows[offset:offset + limit]


@router.get("/summary")
async def match_review_summary(
    legal_entity_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    """Outstanding-decision counts for the queue tabs, plus the actionable exception count."""
    resolved_ids = await _scoped_entity_ids(db, user, legal_entity_id)
    pending = ["PENDING"]
    s_stmt = _suggestion_stmt(resolved_ids, pending, run_id, legal_entity_id)
    g_stmt = _group_stmt(resolved_ids, pending, run_id, legal_entity_id, None)
    pending_suggestions = (await db.execute(select(func.count()).select_from(s_stmt.subquery()))).scalar_one()
    pending_groups = (await db.execute(select(func.count()).select_from(g_stmt.subquery()))).scalar_one()
    items = apply_resolved_entity_scope(
        select(func.count()).select_from(ReconciliationOpenItem), ReconciliationOpenItem.legal_entity_id, resolved_ids)
    items = items.where(ReconciliationOpenItem.status.in_((OpenItemStatus.OPEN, OpenItemStatus.UNDER_REVIEW)))
    if legal_entity_id:
        items = items.where(ReconciliationOpenItem.legal_entity_id == legal_entity_id)
    if run_id:
        items = items.where(ReconciliationOpenItem.reconciliation_run_id == run_id)
    return {"pending_decisions": pending_suggestions + pending_groups, "pending_suggestions": pending_suggestions,
            "pending_groups": pending_groups, "open_exceptions": (await db.execute(items)).scalar_one()}


def _bank_dict(b: BankStatementTransaction) -> dict:
    return {"id": str(b.id), "transaction_date": b.transaction_date.isoformat(), "entry_type": b.entry_type.value,
            "amount": str(b.amount), "currency_code": b.currency_code, "bank_reference": b.bank_reference,
            "narration": b.narration, "status": b.status.value}


def _ledger_dict(t: TreasuryTransaction) -> dict:
    return {"id": str(t.id), "event_date": t.event_date.isoformat(), "direction": t.direction.value,
            "amount": str(t.transaction_amount), "currency_code": t.transaction_currency_code,
            "reference": t.reference, "narration": t.narration, "status": t.status.value}


async def _authorized_proposal(db, user, kind: MatchProposalKind, proposal_id, action: TreasuryAction):
    proposal = await decisions.load_proposal(db, kind, proposal_id)
    run = await db.get(R, proposal.reconciliation_run_id) if proposal else None
    if proposal is None or run is None or not await _can_view(db, user, run.legal_entity_id):
        raise HTTPException(status_code=404, detail="Match proposal not found.")
    if action != TreasuryAction.VIEW:
        await assert_entity_access(db, user, MODULE, action, run.legal_entity_id)
    return proposal, run


@router.get("/{kind}/{proposal_id}")
async def get_match_proposal(
    kind: MatchProposalKind, proposal_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    proposal, run = await _authorized_proposal(db, user, kind, proposal_id, TreasuryAction.VIEW)
    found = await _decisions_for(
        db, [proposal.id] if kind == MatchProposalKind.SUGGESTION else [],
        [proposal.id] if kind == MatchProposalKind.GROUP else [])
    out: dict = {
        "kind": kind.value, "id": str(proposal.id), "reconciliation_run_id": str(run.id),
        "legal_entity_id": str(run.legal_entity_id), "bank_account_id": str(run.bank_account_id),
        "status": proposal.status.value, "reason": proposal.reason,
        "decision": _decision_dict(found.get((kind.value, proposal.id))),
        "bank_transactions": [], "ledger_transactions": [], "not_decidable_reason": None, "acceptance_conflict": None,
    }
    if kind == MatchProposalKind.SUGGESTION:
        out.update({"label": "ONE_TO_ONE", "confidence": str(proposal.confidence) if proposal.confidence is not None else None,
                    "match_type": proposal.match_type.value if proposal.match_type else None,
                    "matching_rule_version": proposal.matching_rule_version})
        if proposal.bank_statement_transaction_id:
            bank = await db.get(BankStatementTransaction, proposal.bank_statement_transaction_id)
            out["bank_transactions"] = [_bank_dict(bank)] if bank else []
        if proposal.treasury_transaction_id:
            ledger = await db.get(TreasuryTransaction, proposal.treasury_transaction_id)
            out["ledger_transactions"] = [_ledger_dict(ledger)] if ledger else []
        if proposal.status == MatchSuggestionStatus.PENDING:
            out["not_decidable_reason"] = await decisions.suggestion_not_decidable_reason(db, proposal)
    else:
        out.update({
            "label": proposal.relationship_type.value, "confidence": str(proposal.confidence),
            "matching_rule_version": proposal.matching_rule_version, "currency_code": proposal.currency_code,
            "bank_aggregate_amount": str(proposal.bank_aggregate_amount),
            "ledger_aggregate_amount": str(proposal.ledger_aggregate_amount), "difference": str(proposal.difference),
            "fx": None if proposal.fx_rate is None else {
                "source_currency": proposal.fx_source_currency_code, "target_currency": proposal.fx_target_currency_code,
                "rate": str(proposal.fx_rate), "rate_date": proposal.fx_rate_date.isoformat() if proposal.fx_rate_date else None,
                "rate_type": proposal.fx_rate_type, "rate_source": proposal.fx_rate_source,
                "tolerance_pct": str(proposal.fx_tolerance_pct) if proposal.fx_tolerance_pct is not None else None,
            },
        })
        members = (await db.execute(select(ReconciliationMatchGroupMember).where(
            ReconciliationMatchGroupMember.match_group_id == proposal.id))).scalars().all()
        bank_ids = [m.bank_statement_transaction_id for m in members if m.bank_statement_transaction_id]
        ledger_ids = [m.treasury_transaction_id for m in members if m.treasury_transaction_id]
        if bank_ids:
            out["bank_transactions"] = [_bank_dict(b) for b in (await db.execute(
                select(BankStatementTransaction).where(BankStatementTransaction.id.in_(bank_ids))
                .order_by(BankStatementTransaction.transaction_date, BankStatementTransaction.id))).scalars()]
        if ledger_ids:
            out["ledger_transactions"] = [_ledger_dict(t) for t in (await db.execute(
                select(TreasuryTransaction).where(TreasuryTransaction.id.in_(ledger_ids))
                .order_by(TreasuryTransaction.event_date, TreasuryTransaction.id))).scalars()]
        if proposal.status == MatchGroupStatus.AMBIGUOUS:
            out["not_decidable_reason"] = "Competing ambiguous groups are exceptions to investigate from the open-items queue."
    if out["status"] == "PENDING" and not out["not_decidable_reason"]:
        # Surfaces, BEFORE the user clicks, that accepting would fail: an overlapping proposal (any run) was already
        # accepted, or a member transaction is no longer eligible. Rejecting such a proposal remains possible.
        out["acceptance_conflict"] = await decisions.acceptance_blocker(db, kind, proposal, run)
    return out


async def _decide(db, user, kind, proposal_id, decision: MatchDecisionType, reason):
    await _authorized_proposal(db, user, kind, proposal_id, TreasuryAction.APPROVE)
    result = await decisions.decide_proposal(db, kind=kind, proposal_id=proposal_id, actor=user,
                                             decision=decision, reason=reason)
    await db.commit()
    return {"kind": kind.value, "id": str(proposal_id), "status": result.decision.new_status,
            "already_decided": result.already_decided, "decision": _decision_dict(result.decision)}


@router.post("/{kind}/{proposal_id}/accept")
async def accept_match_proposal(
    kind: MatchProposalKind, proposal_id: uuid.UUID, payload: DecisionRequest | None = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    return await _decide(db, user, kind, proposal_id, MatchDecisionType.ACCEPTED, payload.reason if payload else None)


@router.post("/{kind}/{proposal_id}/reject")
async def reject_match_proposal(
    kind: MatchProposalKind, proposal_id: uuid.UUID, payload: DecisionRequest | None = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    return await _decide(db, user, kind, proposal_id, MatchDecisionType.REJECTED, payload.reason if payload else None)
