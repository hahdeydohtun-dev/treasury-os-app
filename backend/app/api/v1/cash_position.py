import datetime
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import get_authorized_scope, resolve_scope_entity_ids
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.services.cash_position_service import calculate_cash_position

router = APIRouter(prefix="/cash-position", tags=["cash-position"])


class CashPositionOut(BaseModel):
    as_of: datetime.date
    account_count: int
    total_cash: Decimal
    available_cash: Decimal
    overdraft: Decimal
    net_cash: Decimal
    by_currency: dict[str, Decimal]


@router.get("", response_model=CashPositionOut)
async def get_cash_position(
    legal_entity_id: uuid.UUID | None = None,
    bank_id: uuid.UUID | None = None,
    bank_account_id: uuid.UUID | None = None,
    currency_code: str | None = None,
    as_of: datetime.date | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CashPositionOut:
    """
    Group/entity/currency/bank/account cash position (SECTION 20). Omit
    all filters for the group-wide position; pass `legal_entity_id` for an
    entity view, `currency_code` for a currency view, etc. Filters combine
    (e.g. entity + currency together).

    Server-side scoping (Stage 2 Hardening): this is exactly the
    "aggregate endpoint can leak sensitive information even when
    individual records are hidden" case the hardening pass targets. An
    entity-scoped user's group-wide (no filter) request is now restricted
    to their own authorized entities' cash, never the whole system's;
    a group-wide user's request is restricted to their authorized
    group(s)' entities, never every group in the database.
    """
    scope = await get_authorized_scope(db, user, TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No CASH_LIQUIDITY:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    entity_ids_filter = None if resolved_ids == "ALL" else list(resolved_ids)

    result = await calculate_cash_position(
        db,
        legal_entity_id=legal_entity_id,
        legal_entity_ids=entity_ids_filter,
        bank_id=bank_id,
        bank_account_id=bank_account_id,
        currency_code=currency_code,
        as_of=as_of,
    )
    return CashPositionOut(
        as_of=as_of or datetime.date.today(),
        account_count=result.account_count,
        total_cash=result.total_cash,
        available_cash=result.available_cash,
        overdraft=result.overdraft,
        net_cash=result.net_cash,
        by_currency=result.by_currency,
    )
