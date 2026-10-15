"""
Stage 5E: reconciliation open-item work queue API.

Authorization is entirely server-side and reuses the existing BANK_RECONCILIATION
permission actions: VIEW (list/detail/history), ASSIGN, INVESTIGATE (start),
COMMENT (comments + evidence references), RESOLVE, CLOSE (close / reject / reopen)
and EXECUTE (synchronize a run). An item the caller cannot VIEW is reported as
404, never 403, so item IDs of another entity cannot be probed.
"""
import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.authorization import (
    apply_resolved_entity_scope,
    assert_entity_access,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.bank_statement import BankStatementTransaction
from app.models.rbac import Role, TreasuryAction, TreasuryModule, User, UserRoleAssignment
from app.models.reconciliation import (
    MatchGroupStatus,
    OpenItemCategory,
    OpenItemPriority,
    OpenItemStatus,
    ReconciliationMatchGroup,
    ReconciliationMatchGroupMember,
    ReconciliationMatchSuggestion,
    ReconciliationOpenItem,
    ReconciliationOpenItemEvent,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.models.treasury_transaction import TreasuryTransaction
from app.schemas.reconciliation import (
    OpenItemAssignRequest,
    OpenItemCommentRequest,
    OpenItemDetailOut,
    OpenItemEventOut,
    OpenItemEvidenceRequest,
    OpenItemNoteRequest,
    OpenItemReasonRequest,
    OpenItemSyncOut,
    ReconciliationOpenItemOut,
)
from app.services import reconciliation_open_item_service as svc

router = APIRouter(prefix="/reconciliation", tags=["reconciliation-open-items"])
MODULE = TreasuryModule.BANK_RECONCILIATION
A = TreasuryAction


def _with_roles(stmt):
    """Eager-load exactly what permission resolution reads (assignments -> role -> permissions).
    The authenticated caller arrives with this loaded; any OTHER user whose permissions we evaluate
    (an assignee) must be loaded the same way, because lazy loading is not available under async."""
    return stmt.options(
        selectinload(User.role_assignments).selectinload(UserRoleAssignment.role).selectinload(Role.permissions)
    ).execution_options(populate_existing=True)


async def _can_view(db: AsyncSession, user: User, entity_id: uuid.UUID) -> bool:
    try:
        await assert_entity_access(db, user, MODULE, A.VIEW, entity_id)
        return True
    except HTTPException:
        return False


async def _authorized_item(
    db: AsyncSession, user: User, item_id: uuid.UUID, action: TreasuryAction, *, lock: bool = False,
) -> ReconciliationOpenItem:
    """Load the item, hiding existence from callers outside its scope, then require `action`."""
    item = await (svc.load_item_for_update(db, item_id) if lock else db.get(ReconciliationOpenItem, item_id))
    if item is None or not await _can_view(db, user, item.legal_entity_id):
        raise HTTPException(status_code=404, detail="Open item not found.")
    if action != A.VIEW:
        await assert_entity_access(db, user, MODULE, action, item.legal_entity_id)
    return item


def _age_days(item: ReconciliationOpenItem) -> int:
    if item.status in svc.ACTIONABLE_STATUSES:
        end = datetime.date.today()
    else:
        stamp = item.closed_at or item.rejected_at or item.resolved_at
        end = stamp.date() if stamp else datetime.date.today()
    return max((end - item.item_date).days, 0)


def _out(item: ReconciliationOpenItem, model=ReconciliationOpenItemOut):
    out = model.model_validate(item)
    out.age_days = _age_days(item)
    return out


@router.get("/open-items", response_model=list[ReconciliationOpenItemOut])
async def list_open_items(
    response: Response,
    legal_entity_id: uuid.UUID | None = None,
    bank_account_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    category: list[OpenItemCategory] | None = Query(None),
    status: list[OpenItemStatus] | None = Query(None, description="Default: OPEN and UNDER_REVIEW only."),
    priority: OpenItemPriority | None = None,
    assigned_to_user_id: uuid.UUID | None = None,
    unassigned: bool = False,
    min_age_days: int | None = Query(None, ge=0),
    max_age_days: int | None = Query(None, ge=0),
    min_amount: float | None = Query(None, ge=0),
    max_amount: float | None = Query(None, ge=0),
    currency_code: str | None = Query(None, min_length=3, max_length=3),
    sort: str = Query("oldest", pattern="^(oldest|amount_desc|priority)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    scope = await get_authorized_scope(db, user, MODULE, A.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No BANK_RECONCILIATION:VIEW permission.")
    if legal_entity_id is not None and not await _can_view(db, user, legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    stmt = apply_resolved_entity_scope(
        select(ReconciliationOpenItem), ReconciliationOpenItem.legal_entity_id,
        await resolve_scope_entity_ids(db, scope),
    )
    # The default work queue is ACTIONABLE items only; resolved/closed/rejected history needs an explicit filter.
    stmt = stmt.where(ReconciliationOpenItem.status.in_(status or list(svc.ACTIONABLE_STATUSES)))
    if legal_entity_id:
        stmt = stmt.where(ReconciliationOpenItem.legal_entity_id == legal_entity_id)
    if bank_account_id:
        stmt = stmt.where(ReconciliationOpenItem.bank_account_id == bank_account_id)
    if run_id:
        stmt = stmt.where(ReconciliationOpenItem.reconciliation_run_id == run_id)
    if category:
        stmt = stmt.where(ReconciliationOpenItem.category.in_(category))
    if priority:
        stmt = stmt.where(ReconciliationOpenItem.priority == priority)
    if assigned_to_user_id:
        stmt = stmt.where(ReconciliationOpenItem.assigned_to_user_id == assigned_to_user_id)
    if unassigned:
        stmt = stmt.where(ReconciliationOpenItem.assigned_to_user_id.is_(None))
    today = datetime.date.today()
    if min_age_days is not None:
        stmt = stmt.where(ReconciliationOpenItem.item_date <= today - datetime.timedelta(days=min_age_days))
    if max_age_days is not None:
        stmt = stmt.where(ReconciliationOpenItem.item_date >= today - datetime.timedelta(days=max_age_days))
    if min_amount is not None:
        stmt = stmt.where(ReconciliationOpenItem.amount >= min_amount)
    if max_amount is not None:
        stmt = stmt.where(ReconciliationOpenItem.amount <= max_amount)
    if currency_code:
        stmt = stmt.where(ReconciliationOpenItem.currency_code == currency_code.upper())

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    response.headers["X-Total-Count"] = str(total)

    rank = case((ReconciliationOpenItem.priority == OpenItemPriority.HIGH, 0),
                (ReconciliationOpenItem.priority == OpenItemPriority.MEDIUM, 1), else_=2)
    ordering = {
        "oldest": (ReconciliationOpenItem.item_date.asc(), ReconciliationOpenItem.id.asc()),
        "amount_desc": (ReconciliationOpenItem.amount.desc(), ReconciliationOpenItem.id.asc()),
        "priority": (rank.asc(), ReconciliationOpenItem.item_date.asc(), ReconciliationOpenItem.id.asc()),
    }[sort]
    items = (await db.execute(stmt.order_by(*ordering).limit(limit).offset(offset))).scalars().all()
    return [_out(i) for i in items]


@router.get("/open-items/assignable-users")
async def list_assignable_users(
    legal_entity_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> list[dict]:
    """Active users who could actually work an item for this entity (INVESTIGATE on the entity).
    Only callers allowed to ASSIGN on the entity can read this list."""
    await assert_entity_access(db, user, MODULE, A.ASSIGN, legal_entity_id)
    candidates = (await db.execute(
        _with_roles(select(User).where(User.is_active.is_(True)).order_by(User.full_name).limit(500))
    )).scalars().all()
    result = []
    for candidate in candidates:
        try:
            await assert_entity_access(db, candidate, MODULE, A.INVESTIGATE, legal_entity_id)
        except HTTPException:
            continue
        result.append({"id": str(candidate.id), "full_name": candidate.full_name})
    return result


@router.get("/open-items/{item_id}", response_model=OpenItemDetailOut)
async def get_open_item(
    item_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> OpenItemDetailOut:
    item = await _authorized_item(db, user, item_id, A.VIEW)
    detail = _out(item, OpenItemDetailOut)

    if item.bank_statement_transaction_id:
        bank = await db.get(BankStatementTransaction, item.bank_statement_transaction_id)
        if bank:
            detail.bank_transaction = {
                "id": str(bank.id), "transaction_date": bank.transaction_date.isoformat(),
                "entry_type": bank.entry_type.value, "amount": str(bank.amount), "currency_code": bank.currency_code,
                "bank_reference": bank.bank_reference, "narration": bank.narration, "status": bank.status.value,
            }
    if item.treasury_transaction_id:
        ledger = await db.get(TreasuryTransaction, item.treasury_transaction_id)
        if ledger:
            detail.ledger_transaction = {
                "id": str(ledger.id), "event_date": ledger.event_date.isoformat(), "direction": ledger.direction.value,
                "amount": str(ledger.transaction_amount), "currency_code": ledger.transaction_currency_code,
                "reference": ledger.reference, "narration": ledger.narration, "status": ledger.status.value,
            }

    suggestion_filters = []
    if item.bank_statement_transaction_id:
        suggestion_filters.append(ReconciliationMatchSuggestion.bank_statement_transaction_id == item.bank_statement_transaction_id)
    if item.treasury_transaction_id:
        suggestion_filters.append(ReconciliationMatchSuggestion.treasury_transaction_id == item.treasury_transaction_id)
    if suggestion_filters:
        from sqlalchemy import or_
        suggestions = (await db.execute(
            select(ReconciliationMatchSuggestion).where(
                ReconciliationMatchSuggestion.reconciliation_run_id == item.reconciliation_run_id, or_(*suggestion_filters),
            ).order_by(ReconciliationMatchSuggestion.confidence.desc().nullslast(), ReconciliationMatchSuggestion.id)
        )).scalars().all()
        detail.suggestions = [{
            "id": str(s.id), "status": s.status.value, "match_type": s.match_type.value if s.match_type else None,
            "confidence": str(s.confidence) if s.confidence is not None else None, "reason": s.reason,
            "treasury_transaction_id": str(s.treasury_transaction_id) if s.treasury_transaction_id else None,
        } for s in suggestions]

    member_filters = []
    if item.bank_statement_transaction_id:
        member_filters.append(ReconciliationMatchGroupMember.bank_statement_transaction_id == item.bank_statement_transaction_id)
    if item.treasury_transaction_id:
        member_filters.append(ReconciliationMatchGroupMember.treasury_transaction_id == item.treasury_transaction_id)
    if member_filters:
        from sqlalchemy import or_
        groups = (await db.execute(
            select(ReconciliationMatchGroup).join(
                ReconciliationMatchGroupMember, ReconciliationMatchGroupMember.match_group_id == ReconciliationMatchGroup.id
            ).where(ReconciliationMatchGroup.reconciliation_run_id == item.reconciliation_run_id, or_(*member_filters))
            .distinct()
        )).scalars().all()
        detail.match_groups = [{
            "id": str(g.id), "relationship_type": g.relationship_type.value, "status": g.status.value,
            "reason": g.reason, "ambiguous": g.status == MatchGroupStatus.AMBIGUOUS,
        } for g in groups]
    return detail


@router.get("/open-items/{item_id}/history", response_model=list[OpenItemEventOut])
async def get_open_item_history(
    item_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> list:
    item = await _authorized_item(db, user, item_id, A.VIEW)
    return list((await db.execute(
        select(ReconciliationOpenItemEvent).where(ReconciliationOpenItemEvent.open_item_id == item.id)
        .order_by(ReconciliationOpenItemEvent.created_at, ReconciliationOpenItemEvent.id)
    )).scalars().all())


async def _act(db: AsyncSession, user: User, item_id: uuid.UUID, action: TreasuryAction, fn, *args, **kwargs):
    """Authorize, row-lock, apply the state change, commit - one transaction per action. A
    conflicting concurrent action blocks on the lock and then fails its transition check."""
    item = await _authorized_item(db, user, item_id, action, lock=True)
    await fn(db, item, user, *args, **kwargs)
    await db.commit()
    await db.refresh(item)
    return _out(item)


@router.post("/open-items/{item_id}/assign", response_model=ReconciliationOpenItemOut)
async def assign_open_item(
    item_id: uuid.UUID, payload: OpenItemAssignRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = await _authorized_item(db, user, item_id, A.ASSIGN, lock=True)
    assignee = (await db.execute(_with_roles(select(User).where(User.id == payload.assignee_user_id)))).scalar_one_or_none()
    if assignee is None or not assignee.is_active:
        raise HTTPException(status_code=400, detail="Assignee must be an existing, active user.")
    try:
        await assert_entity_access(db, assignee, MODULE, A.INVESTIGATE, item.legal_entity_id)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Assignee is not authorized to work items for this entity.") from None
    await svc.assign_item(db, item, user, assignee, payload.reason, payload.due_date, payload.priority)
    await db.commit()
    await db.refresh(item)
    return _out(item)


@router.post("/open-items/{item_id}/start-investigation", response_model=ReconciliationOpenItemOut)
async def start_open_item_investigation(
    item_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    return await _act(db, user, item_id, A.INVESTIGATE, svc.start_investigation)


@router.post("/open-items/{item_id}/comments", response_model=ReconciliationOpenItemOut)
async def comment_on_open_item(
    item_id: uuid.UUID, payload: OpenItemCommentRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _act(db, user, item_id, A.COMMENT, svc.add_comment, payload.note)


@router.post("/open-items/{item_id}/evidence", response_model=ReconciliationOpenItemOut)
async def add_open_item_evidence(
    item_id: uuid.UUID, payload: OpenItemEvidenceRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _act(db, user, item_id, A.COMMENT, svc.add_evidence, payload.reference, payload.note)


@router.post("/open-items/{item_id}/resolve", response_model=ReconciliationOpenItemOut)
async def resolve_open_item(
    item_id: uuid.UUID, payload: OpenItemReasonRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _act(db, user, item_id, A.RESOLVE, svc.resolve_item, payload.reason)


@router.post("/open-items/{item_id}/close", response_model=ReconciliationOpenItemOut)
async def close_open_item(
    item_id: uuid.UUID, payload: OpenItemNoteRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _act(db, user, item_id, A.CLOSE, svc.close_item, payload.note)


@router.post("/open-items/{item_id}/reject", response_model=ReconciliationOpenItemOut)
async def reject_open_item(
    item_id: uuid.UUID, payload: OpenItemReasonRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _act(db, user, item_id, A.CLOSE, svc.reject_item, payload.reason)


@router.post("/open-items/{item_id}/reopen", response_model=ReconciliationOpenItemOut)
async def reopen_open_item(
    item_id: uuid.UUID, payload: OpenItemReasonRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _act(db, user, item_id, A.CLOSE, svc.reopen_item, payload.reason)


@router.post("/runs/{run_id}/open-items/sync", response_model=OpenItemSyncOut)
async def sync_run_open_items(
    run_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """(Re)synchronize open items for a COMPLETED run. Idempotent: nothing already derived is duplicated.
    Items are normally created automatically when a run executes; this exists for retries/backfill."""
    run = await db.get(ReconciliationRun, run_id)
    if run is None or not await _can_view(db, user, run.legal_entity_id):
        raise HTTPException(status_code=404, detail="Reconciliation run not found.")
    await assert_entity_access(db, user, MODULE, A.EXECUTE, run.legal_entity_id)
    if run.status != ReconciliationRunStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Open items can only be synchronized for a COMPLETED run.")
    result = await svc.sync_open_items_for_run(db, run, user.id)
    await db.commit()
    return OpenItemSyncOut(
        reconciliation_run_id=run.id, created=result.created, already_existing=result.existing,
        by_category=result.by_category,
    )
