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
from app.auth.dependencies import get_current_user, require_permission
from app.db.session import get_db
from app.models.banking import Bank, BankAccount
from app.models.lookup import AccountType, CashEventType
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.banking import (
    AccountTypeOut,
    BankAccountCreate,
    BankAccountOut,
    BankCreate,
    BankOut,
    CashEventTypeOut,
)
from app.services.audit_service import record_audit_event

router = APIRouter(tags=["banking"])


def _to_bank_account_out(account: BankAccount) -> BankAccountOut:
    return BankAccountOut(
        id=account.id,
        legal_entity_id=account.legal_entity_id,
        bank_id=account.bank_id,
        account_name=account.account_name,
        account_number_masked=BankAccountOut.mask_account_number(account.account_number),
        currency_code=account.currency_code,
        account_type_code=account.account_type_code,
        status=account.status,
        opening_date=account.opening_date,
        closing_date=account.closing_date,
        minimum_operating_balance=account.minimum_operating_balance,
        overdraft_limit=account.overdraft_limit,
        is_active=account.is_active,
    )


@router.get("/account-types", response_model=list[AccountTypeOut])
async def list_account_types(db: AsyncSession = Depends(get_db)) -> list[AccountType]:
    result = await db.execute(select(AccountType).where(AccountType.is_active.is_(True)))
    return list(result.scalars().all())


@router.get("/cash-event-types", response_model=list[CashEventTypeOut])
async def list_cash_event_types(db: AsyncSession = Depends(get_db)) -> list[CashEventType]:
    result = await db.execute(select(CashEventType).where(CashEventType.is_active.is_(True)))
    return list(result.scalars().all())


@router.post("/banks", response_model=BankOut, status_code=status.HTTP_201_CREATED)
async def create_bank(
    payload: BankCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(TreasuryModule.BANKS_ACCOUNTS, TreasuryAction.CREATE)),
) -> Bank:
    bank = Bank(**payload.model_dump())
    db.add(bank)
    await db.flush()
    await record_audit_event(
        db, module=TreasuryModule.BANKS_ACCOUNTS.value, action="CREATE",
        record_type="Bank", record_id=str(bank.id), user_id=user.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(bank)
    return bank


@router.get("/banks", response_model=list[BankOut])
async def list_banks(db: AsyncSession = Depends(get_db)) -> list[Bank]:
    result = await db.execute(select(Bank).where(Bank.is_active.is_(True)))
    return list(result.scalars().all())


@router.post(
    "/bank-accounts", response_model=BankAccountOut, status_code=status.HTTP_201_CREATED
)
async def create_bank_account(
    payload: BankAccountCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankAccountOut:
    """
    Entity comes from the request body, not the URL, so the generic
    `require_permission` dependency (which only resolves entity_id from
    path/query params) can't be used directly here - it would reject
    entity-scoped users outright since it would see no entity_id at all.
    Instead we authenticate via `get_current_user` and check the
    permission explicitly against `payload.legal_entity_id`.
    """
    await assert_entity_access(
        db, user, TreasuryModule.BANKS_ACCOUNTS, TreasuryAction.CREATE, payload.legal_entity_id,
    )

    account = BankAccount(**payload.model_dump())
    db.add(account)
    await db.flush()
    await record_audit_event(
        db, module=TreasuryModule.BANKS_ACCOUNTS.value, action="CREATE",
        record_type="BankAccount", record_id=str(account.id), user_id=user.id,
        legal_entity_id=account.legal_entity_id,
        new_value=payload.model_dump(mode="json", exclude={"account_number"}),
    )
    await db.commit()
    await db.refresh(account)
    return _to_bank_account_out(account)


@router.get("/bank-accounts", response_model=list[BankAccountOut])
async def list_bank_accounts(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[BankAccountOut]:
    scope = await get_authorized_scope(db, user, TreasuryModule.BANKS_ACCOUNTS, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No BANKS_ACCOUNTS:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(BankAccount).where(BankAccount.is_active.is_(True))
    stmt = apply_resolved_entity_scope(stmt, BankAccount.legal_entity_id, resolved_ids)
    if legal_entity_id:
        stmt = stmt.where(BankAccount.legal_entity_id == legal_entity_id)
    result = await db.execute(stmt)
    return [_to_bank_account_out(a) for a in result.scalars().all()]


@router.get("/bank-accounts/{account_id}", response_model=BankAccountOut)
async def get_bank_account(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankAccountOut:
    account = await db.get(BankAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    await assert_entity_access(
        db, user, TreasuryModule.BANKS_ACCOUNTS, TreasuryAction.VIEW, account.legal_entity_id,
    )
    return _to_bank_account_out(account)
