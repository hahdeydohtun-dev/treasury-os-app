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
from app.models.bank_charge import BankCharge
from app.models.expected_cash_flow import ExpectedCollection, ExpectedPayment
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.expected_cash_flow import (
    BankChargeCreate,
    BankChargeOut,
    ExpectedCollectionCreate,
    ExpectedCollectionOut,
    ExpectedPaymentCreate,
    ExpectedPaymentOut,
)
from app.services.audit_service import record_audit_event

router = APIRouter(tags=["forecast-inputs"])


async def _list_with_scope(
    db: AsyncSession, user: User, model, order_column, legal_entity_id: uuid.UUID | None
):
    """
    Shared server-side scoping for the three list endpoints below: resolve
    the caller's authorized entities (translating any GROUP_WIDE grant
    into concrete entity ids), reject an explicitly-requested entity_id
    that isn't authorized, and apply the scope as a WHERE clause before
    any rows are fetched.
    """
    scope = await get_authorized_scope(db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No CASH_LIQUIDITY:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(model).order_by(order_column)
    stmt = apply_resolved_entity_scope(stmt, model.legal_entity_id, resolved_ids)
    if legal_entity_id:
        stmt = stmt.where(model.legal_entity_id == legal_entity_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# --- Expected Collections ---
@router.post(
    "/expected-collections", response_model=ExpectedCollectionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_expected_collection(
    payload: ExpectedCollectionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExpectedCollection:
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.CREATE, payload.legal_entity_id,
    )
    item = ExpectedCollection(**payload.model_dump())
    db.add(item)
    await db.flush()
    await record_audit_event(
        db, module=TreasuryModule.CASH_LIQUIDITY.value, action="CREATE",
        record_type="ExpectedCollection", record_id=str(item.id), user_id=user.id,
        legal_entity_id=item.legal_entity_id, new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.get("/expected-collections", response_model=list[ExpectedCollectionOut])
async def list_expected_collections(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ExpectedCollection]:
    return await _list_with_scope(
        db, user, ExpectedCollection, ExpectedCollection.expected_date, legal_entity_id,
    )


@router.get("/expected-collections/{item_id}", response_model=ExpectedCollectionOut)
async def get_expected_collection(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExpectedCollection:
    item = await db.get(ExpectedCollection, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Expected collection not found")
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW, item.legal_entity_id,
    )
    return item


# --- Expected Payments ---
@router.post(
    "/expected-payments", response_model=ExpectedPaymentOut, status_code=status.HTTP_201_CREATED
)
async def create_expected_payment(
    payload: ExpectedPaymentCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExpectedPayment:
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.CREATE, payload.legal_entity_id,
    )
    item = ExpectedPayment(**payload.model_dump())
    db.add(item)
    await db.flush()
    await record_audit_event(
        db, module=TreasuryModule.CASH_LIQUIDITY.value, action="CREATE",
        record_type="ExpectedPayment", record_id=str(item.id), user_id=user.id,
        legal_entity_id=item.legal_entity_id, new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.get("/expected-payments", response_model=list[ExpectedPaymentOut])
async def list_expected_payments(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ExpectedPayment]:
    return await _list_with_scope(
        db, user, ExpectedPayment, ExpectedPayment.expected_date, legal_entity_id,
    )


@router.get("/expected-payments/{item_id}", response_model=ExpectedPaymentOut)
async def get_expected_payment(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExpectedPayment:
    item = await db.get(ExpectedPayment, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Expected payment not found")
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW, item.legal_entity_id,
    )
    return item


# --- Bank Charges ---
@router.post("/bank-charges", response_model=BankChargeOut, status_code=status.HTTP_201_CREATED)
async def create_bank_charge(
    payload: BankChargeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankCharge:
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.CREATE, payload.legal_entity_id,
    )
    item = BankCharge(**payload.model_dump())
    db.add(item)
    await db.flush()
    await record_audit_event(
        db, module=TreasuryModule.CASH_LIQUIDITY.value, action="CREATE",
        record_type="BankCharge", record_id=str(item.id), user_id=user.id,
        legal_entity_id=item.legal_entity_id, new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.get("/bank-charges", response_model=list[BankChargeOut])
async def list_bank_charges(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[BankCharge]:
    return await _list_with_scope(db, user, BankCharge, BankCharge.charge_date.desc(), legal_entity_id)


@router.get("/bank-charges/{item_id}", response_model=BankChargeOut)
async def get_bank_charge(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankCharge:
    item = await db.get(BankCharge, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Bank charge not found")
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW, item.legal_entity_id,
    )
    return item
