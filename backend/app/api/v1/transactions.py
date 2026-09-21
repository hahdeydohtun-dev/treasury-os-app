import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    apply_resolved_entity_scope,
    assert_entity_access,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.lookup import CashDirection, CashEventType
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.models.treasury_transaction import TreasuryTransaction
from app.schemas.treasury_transaction import TreasuryTransactionCreate, TreasuryTransactionOut
from app.services.audit_service import record_audit_event

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.post("", response_model=TreasuryTransactionOut, status_code=status.HTTP_201_CREATED)
async def create_transaction(
    payload: TreasuryTransactionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TreasuryTransaction:
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.CREATE, payload.legal_entity_id
    )

    event_type = await db.get(CashEventType, payload.event_type_code)
    if event_type is None:
        raise HTTPException(status_code=400, detail="Unknown event_type_code.")

    data = payload.model_dump()
    if data.get("direction") is None:
        data["direction"] = event_type.default_direction
    else:
        data["direction"] = CashDirection(data["direction"])

    txn = TreasuryTransaction(**data, created_by_user_id=user.id)
    db.add(txn)
    await db.flush()
    await record_audit_event(
        db, module=TreasuryModule.CASH_LIQUIDITY.value, action="CREATE",
        record_type="TreasuryTransaction", record_id=str(txn.id), user_id=user.id,
        legal_entity_id=txn.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(txn)
    return txn


@router.get("", response_model=list[TreasuryTransactionOut])
async def list_transactions(
    legal_entity_id: uuid.UUID | None = None,
    bank_account_id: uuid.UUID | None = None,
    direction: CashDirection | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TreasuryTransaction]:
    """
    Server-side entity scoping (Stage 2 Hardening): the authorized scope
    is resolved once and applied as a WHERE clause. An entity-scoped user
    with no filter sees only their own authorized entities' transactions,
    never a 403 and never another entity's data; requesting an
    unauthorized entity_id is rejected outright rather than silently
    ignored or widened.
    """
    scope = await get_authorized_scope(db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No CASH_LIQUIDITY:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(TreasuryTransaction).order_by(TreasuryTransaction.event_date.desc())
    stmt = apply_resolved_entity_scope(stmt, TreasuryTransaction.legal_entity_id, resolved_ids)
    if legal_entity_id:
        stmt = stmt.where(TreasuryTransaction.legal_entity_id == legal_entity_id)
    if bank_account_id:
        stmt = stmt.where(TreasuryTransaction.bank_account_id == bank_account_id)
    if direction:
        stmt = stmt.where(TreasuryTransaction.direction == direction)
    stmt = stmt.limit(min(limit, 500))
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{transaction_id}", response_model=TreasuryTransactionOut)
async def get_transaction(
    transaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TreasuryTransaction:
    txn = await db.get(TreasuryTransaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    # Detail-level authorization: verified against the record's OWN
    # entity, never derived from the request (SECTION 7).
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW, txn.legal_entity_id
    )
    return txn
