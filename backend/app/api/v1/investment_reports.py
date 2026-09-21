"""
Investment liquidity, concentration, alerts, and maturity-calendar
endpoints (SECTIONS 21-25, 36-37).

Every endpoint resolves the caller's authorized entity scope first (the
Stage 2 hardened authorization module) and applies it to every
underlying query - the same discipline established for the funding
aggregate endpoints (app/api/v1/funding.py).
"""
import datetime
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import get_authorized_scope, resolve_scope_entity_ids
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.investment import Investment, InvestmentConcentrationLimit, InvestmentStatus
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.investment import (
    ConcentrationRow,
    InvestmentAlertOut,
    InvestmentLiquidityOut,
    InvestmentOut,
)

router = APIRouter(prefix="/investments-reports", tags=["investment-reports"])

MODULE = TreasuryModule.INVESTMENTS

_OUTSTANDING_STATUSES = (InvestmentStatus.ACTIVE, InvestmentStatus.PARTIALLY_TERMINATED)


async def _authorized_entity_ids(db: AsyncSession, user: User, legal_entity_id: uuid.UUID | None = None):
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No INVESTMENTS:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")
    resolved = await resolve_scope_entity_ids(db, scope)
    if legal_entity_id is not None:
        return [legal_entity_id]
    return resolved


async def _outstanding_investments(db: AsyncSession, entity_ids) -> list:
    stmt = select(Investment).where(
        Investment.is_active.is_(True), Investment.status.in_(_OUTSTANDING_STATUSES),
    )
    if entity_ids != "ALL":
        stmt = stmt.where(Investment.legal_entity_id.in_(entity_ids))
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/liquidity", response_model=InvestmentLiquidityOut)
async def investment_liquidity(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InvestmentLiquidityOut:
    """
    SECTION 21/22: cash and invested funds are never treated as
    identical liquidity - this view only ever reports invested amounts
    and their maturity schedule, never a combined "available cash" figure.
    """
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    investments = await _outstanding_investments(db, entity_ids)

    today = datetime.date.today()
    total_invested = Decimal(0)
    maturing_7 = Decimal(0)
    maturing_30 = Decimal(0)
    maturing_90 = Decimal(0)
    total_interest = Decimal(0)
    by_currency: dict = {}
    by_entity: dict = {}
    by_institution: dict = {}
    weighted_rate_numerator = Decimal(0)

    for inv in investments:
        total_invested += inv.principal_amount
        total_interest += inv.expected_interest or Decimal(0)
        weighted_rate_numerator += inv.principal_amount * inv.interest_rate

        days_to_maturity = (inv.maturity_date - today).days
        if 0 <= days_to_maturity <= 7:
            maturing_7 += inv.principal_amount
        if 0 <= days_to_maturity <= 30:
            maturing_30 += inv.principal_amount
        if 0 <= days_to_maturity <= 90:
            maturing_90 += inv.principal_amount

        by_currency[inv.currency_code] = by_currency.get(inv.currency_code, Decimal(0)) + inv.principal_amount
        by_entity[str(inv.legal_entity_id)] = by_entity.get(str(inv.legal_entity_id), Decimal(0)) + inv.principal_amount
        by_institution[str(inv.institution_id)] = by_institution.get(str(inv.institution_id), Decimal(0)) + inv.principal_amount

    weighted_rate = (weighted_rate_numerator / total_invested) if total_invested > 0 else None

    return InvestmentLiquidityOut(
        total_invested=total_invested, maturing_7_days=maturing_7, maturing_30_days=maturing_30,
        maturing_90_days=maturing_90, total_expected_interest=total_interest,
        weighted_average_rate=weighted_rate.quantize(Decimal("0.0001")) if weighted_rate is not None else None,
        investment_count=len(investments), by_currency=by_currency, by_entity=by_entity,
        by_institution=by_institution,
    )


@router.get("/maturity-calendar", response_model=list[InvestmentOut])
async def maturity_calendar(
    start_date: datetime.date | None = None, end_date: datetime.date | None = None,
    legal_entity_id: uuid.UUID | None = None, currency_code: str | None = None,
    institution_id: uuid.UUID | None = None, investment_type_code: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    """SECTION 36: next 7/30/90 days or a custom range, filterable."""
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    start_date = start_date or datetime.date.today()
    end_date = end_date or (start_date + datetime.timedelta(days=30))

    stmt = select(Investment).where(
        Investment.is_active.is_(True), Investment.status.in_(_OUTSTANDING_STATUSES),
        Investment.maturity_date >= start_date, Investment.maturity_date <= end_date,
    )
    if entity_ids != "ALL":
        stmt = stmt.where(Investment.legal_entity_id.in_(entity_ids))
    if currency_code:
        stmt = stmt.where(Investment.currency_code == currency_code.upper())
    if institution_id:
        stmt = stmt.where(Investment.institution_id == institution_id)
    if investment_type_code:
        stmt = stmt.where(Investment.investment_type_code == investment_type_code)
    stmt = stmt.order_by(Investment.maturity_date)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/concentration", response_model=list[ConcentrationRow])
async def investment_concentration(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    """SECTION 23: by institution/entity/currency/type, against configurable limits - never hard-coded."""
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    investments = await _outstanding_investments(db, entity_ids)

    by_institution: dict = {}
    by_currency: dict = {}
    by_type: dict = {}
    for inv in investments:
        by_institution[str(inv.institution_id)] = by_institution.get(str(inv.institution_id), Decimal(0)) + inv.principal_amount
        by_currency[inv.currency_code] = by_currency.get(inv.currency_code, Decimal(0)) + inv.principal_amount
        by_type[inv.investment_type_code] = by_type.get(inv.investment_type_code, Decimal(0)) + inv.principal_amount

    limit_stmt = select(InvestmentConcentrationLimit).where(InvestmentConcentrationLimit.is_active.is_(True))
    limits = list((await db.execute(limit_stmt)).scalars().all())
    limit_by_institution = {str(limit_row.institution_id): limit_row for limit_row in limits if limit_row.institution_id}
    limit_by_currency = {limit_row.currency_code: limit_row for limit_row in limits if limit_row.currency_code}
    limit_by_type = {limit_row.investment_type_code: limit_row for limit_row in limits if limit_row.investment_type_code}

    rows: list = []

    def _build_rows(amounts: dict, dimension: str, limit_map: dict) -> None:
        for key, amount in amounts.items():
            limit_row = limit_map.get(key)
            limit_amount = limit_row.limit_amount if limit_row else None
            available = (limit_amount - amount) if limit_amount is not None else None
            utilization = (amount / limit_amount * 100) if limit_amount and limit_amount > 0 else None
            row_status = "NO_LIMIT_CONFIGURED"
            if limit_amount is not None and limit_row is not None:
                if amount > limit_amount:
                    row_status = "BREACH"
                elif limit_row.warning_threshold_pct is not None and utilization is not None and utilization >= limit_row.warning_threshold_pct:
                    row_status = "WARNING"
                else:
                    row_status = "WITHIN_LIMIT"
            rows.append(ConcentrationRow(
                dimension=dimension, key=key, current_amount=amount, limit_amount=limit_amount,
                available_capacity=available,
                utilization_pct=utilization.quantize(Decimal("0.01")) if utilization is not None else None,
                status=row_status,
            ))

    _build_rows(by_institution, "INSTITUTION", limit_by_institution)
    _build_rows(by_currency, "CURRENCY", limit_by_currency)
    _build_rows(by_type, "INVESTMENT_TYPE", limit_by_type)
    return rows


@router.get("/alerts", response_model=list[InvestmentAlertOut])
async def investment_alerts(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    """SECTION 24: deterministic alerts only - no AI involved."""
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    today = datetime.date.today()
    alerts: list = []

    outstanding = await _outstanding_investments(db, entity_ids)
    for inv in outstanding:
        days = (inv.maturity_date - today).days
        if days == 0:
            alerts.append(InvestmentAlertOut(
                alert_type="MATURITY_TODAY", investment_id=inv.id,
                investment_reference=inv.investment_reference,
                message=f"{inv.investment_reference} matures today.", severity="HIGH",
                due_date=inv.maturity_date,
            ))
        elif 0 < days <= 7:
            alerts.append(InvestmentAlertOut(
                alert_type="MATURITY_APPROACHING", investment_id=inv.id,
                investment_reference=inv.investment_reference,
                message=f"{inv.investment_reference} matures in {days} day(s).", severity="MEDIUM",
                due_date=inv.maturity_date,
            ))
        if inv.rollover_allowed and 0 < days <= 14:
            alerts.append(InvestmentAlertOut(
                alert_type="ROLLOVER_DECISION_REQUIRED", investment_id=inv.id,
                investment_reference=inv.investment_reference,
                message=f"Rollover decision needed for {inv.investment_reference} "
                        f"(matures in {days} day(s)).", severity="MEDIUM", due_date=inv.maturity_date,
            ))

    pending_stmt = select(Investment).where(
        Investment.is_active.is_(True),
        Investment.status.in_((InvestmentStatus.SUBMITTED, InvestmentStatus.UNDER_REVIEW)),
    )
    if entity_ids != "ALL":
        pending_stmt = pending_stmt.where(Investment.legal_entity_id.in_(entity_ids))
    for inv in (await db.execute(pending_stmt)).scalars().all():
        alerts.append(InvestmentAlertOut(
            alert_type="APPROVAL_PENDING", investment_id=inv.id,
            investment_reference=inv.investment_reference,
            message=f"{inv.investment_reference} is awaiting approval ({inv.status.value}).",
            severity="LOW", due_date=None,
        ))

    placement_stmt = select(Investment).where(
        Investment.is_active.is_(True), Investment.status == InvestmentStatus.PLACEMENT_PENDING,
    )
    if entity_ids != "ALL":
        placement_stmt = placement_stmt.where(Investment.legal_entity_id.in_(entity_ids))
    for inv in (await db.execute(placement_stmt)).scalars().all():
        alerts.append(InvestmentAlertOut(
            alert_type="PLACEMENT_PENDING", investment_id=inv.id,
            investment_reference=inv.investment_reference,
            message=f"{inv.investment_reference} is approved and awaiting placement.",
            severity="MEDIUM", due_date=None,
        ))

    return alerts
