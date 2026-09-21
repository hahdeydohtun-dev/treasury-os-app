import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    assert_entity_access,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.balance import BankBalance
from app.models.banking import BankAccount
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.balance import BankBalanceCreate, BankBalanceOut
from app.services.audit_service import record_audit_event

router = APIRouter(prefix="/bank-balances", tags=["bank-balances"])


@router.post("", response_model=BankBalanceOut, status_code=status.HTTP_201_CREATED)
async def create_bank_balance(
    payload: BankBalanceCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankBalance:
    account = await db.get(BankAccount, payload.bank_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    await assert_entity_access(
        db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.CREATE, account.legal_entity_id
    )

    balance = BankBalance(**payload.model_dump())
    db.add(balance)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate balance: an entry for this account/date/source already exists.",
        )
    await record_audit_event(
        db, module=TreasuryModule.CASH_LIQUIDITY.value, action="CREATE",
        record_type="BankBalance", record_id=str(balance.id), user_id=user.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(balance)
    return balance


@router.get("", response_model=list[BankBalanceOut])
async def list_bank_balances(
    bank_account_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[BankBalance]:
    """
    BankBalance has no direct legal_entity_id column - it's scoped via
    its BankAccount. Server-side scoping here is a join against
    BankAccount.legal_entity_id, not a pull-everything-then-filter.
    """
    scope = await get_authorized_scope(db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No CASH_LIQUIDITY:VIEW permission.")

    if bank_account_id is not None:
        account = await db.get(BankAccount, bank_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Bank account not found")
        await assert_entity_access(
            db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW, account.legal_entity_id
        )
        stmt = (
            select(BankBalance)
            .where(BankBalance.bank_account_id == bank_account_id)
            .order_by(BankBalance.balance_date.desc())
        )
    else:
        resolved_ids = await resolve_scope_entity_ids(db, scope)
        stmt = (
            select(BankBalance)
            .join(BankAccount, BankAccount.id == BankBalance.bank_account_id)
            .order_by(BankBalance.balance_date.desc())
        )
        if resolved_ids != "ALL":
            from sqlalchemy import false

            stmt = stmt.where(BankAccount.legal_entity_id.in_(resolved_ids)) if resolved_ids else stmt.where(false())

    result = await db.execute(stmt)
    return list(result.scalars().all())
