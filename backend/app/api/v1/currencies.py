from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_permission
from app.db.session import get_db
from app.models.currency import Currency, FXRate
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.currency import CurrencyCreate, CurrencyOut, FXRateCreate, FXRateOut
from app.services.audit_service import record_audit_event

router = APIRouter(tags=["currency"])


@router.post("/currencies", response_model=CurrencyOut, status_code=status.HTTP_201_CREATED)
async def create_currency(
    payload: CurrencyCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(TreasuryModule.CURRENCY_FX, TreasuryAction.CONFIGURE)),
) -> Currency:
    currency = Currency(**payload.model_dump())
    db.add(currency)
    await db.flush()
    await record_audit_event(
        db,
        module=TreasuryModule.CURRENCY_FX.value,
        action="CREATE",
        record_type="Currency",
        record_id=currency.code,
        user_id=user.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(currency)
    return currency


@router.get("/currencies", response_model=list[CurrencyOut])
async def list_currencies(db: AsyncSession = Depends(get_db)) -> list[Currency]:
    result = await db.execute(select(Currency).where(Currency.is_active.is_(True)))
    return list(result.scalars().all())


@router.post("/fx-rates", response_model=FXRateOut, status_code=status.HTTP_201_CREATED)
async def create_fx_rate(
    payload: FXRateCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(TreasuryModule.CURRENCY_FX, TreasuryAction.CREATE)),
) -> FXRate:
    """
    Creates a new FX rate version. Never updates an existing rate row in
    place (PRINCIPLE 5). If a current rate exists for the same
    (from, to, rate_type, rate_date), it is marked superseded rather than
    overwritten.
    """
    existing_stmt = (
        select(FXRate)
        .where(
            FXRate.from_currency_code == payload.from_currency_code,
            FXRate.to_currency_code == payload.to_currency_code,
            FXRate.rate_type == payload.rate_type,
            FXRate.rate_date == payload.rate_date,
            FXRate.is_current.is_(True),
        )
        .order_by(FXRate.version.desc())
    )
    result = await db.execute(existing_stmt)
    existing = result.scalars().first()

    new_version = (existing.version + 1) if existing else 1
    new_rate = FXRate(
        **payload.model_dump(),
        version=new_version,
        is_current=True,
        created_by_user_id=user.id,
    )
    db.add(new_rate)
    await db.flush()

    if existing:
        existing.is_current = False
        existing.superseded_by_id = new_rate.id

    await record_audit_event(
        db,
        module=TreasuryModule.CURRENCY_FX.value,
        action="CREATE",
        record_type="FXRate",
        record_id=str(new_rate.id),
        user_id=user.id,
        previous_value={"rate": str(existing.rate)} if existing else None,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(new_rate)
    return new_rate


@router.get("/fx-rates", response_model=list[FXRateOut])
async def list_fx_rates(
    current_only: bool = True, db: AsyncSession = Depends(get_db)
) -> list[FXRate]:
    stmt = select(FXRate)
    if current_only:
        stmt = stmt.where(FXRate.is_current.is_(True))
    result = await db.execute(stmt.order_by(FXRate.rate_date.desc()))
    return list(result.scalars().all())
