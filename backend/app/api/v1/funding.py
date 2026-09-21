"""
Funding & Credit Facilities aggregate endpoints (SECTIONS 24-33, 38).

Every endpoint here resolves the caller's authorized entity scope first
(the Stage 2 hardened authorization module) and applies it to every
underlying query - the exact "aggregates can leak information even when
individual records are hidden" risk the hardening pass targeted is
addressed here the same way as Cash Position (SECTION 9 of the hardening
spec, SECTION 49 of this spec).
"""
import datetime
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    assert_entity_access,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.facility import (
    FUNDING_ACTION_STATUS_TRANSITIONS,
    CovenantStatus,
    DrawdownStatus,
    Facility,
    FacilityCovenant,
    FacilityDrawdown,
    FacilityFee,
    FacilityRepayment,
    FacilityStatus,
    FundingAction,
    FundingActionStatus,
    RepaymentStatus,
)
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.facility import (
    FundingActionCreate,
    FundingActionOut,
    FundingCalendarEntry,
    FundingCapacityOut,
    FundingCostReportRow,
    FundingDashboardOut,
    FundingGapOut,
    FundingRecommendationRow,
)
from app.services.audit_service import record_audit_event
from app.services.facility_engine import (
    calculate_funding_cost,
    calculate_utilization,
)
from app.services.funding_action_validation import validate_funding_action
from app.services.funding_capacity_service import calculate_funding_capacity, calculate_funding_gaps

router = APIRouter(prefix="/funding", tags=["funding"])

MODULE = TreasuryModule.FACILITIES


async def _authorized_entity_ids(db: AsyncSession, user: User, legal_entity_id: uuid.UUID | None = None):
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No FACILITIES:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")
    resolved = await resolve_scope_entity_ids(db, scope)
    if legal_entity_id is not None:
        return [legal_entity_id]
    return resolved


@router.get("/dashboard", response_model=FundingDashboardOut)
async def funding_dashboard(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingDashboardOut:
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)

    stmt = select(Facility).where(Facility.is_active.is_(True), Facility.status == FacilityStatus.ACTIVE)
    if entity_ids != "ALL":
        stmt = stmt.where(Facility.legal_entity_id.in_(entity_ids))
    facilities = list((await db.execute(stmt)).scalars().all())

    total_limit = Decimal(0)
    total_drawn = Decimal(0)
    total_undrawn = Decimal(0)
    total_available = Decimal(0)
    by_currency: dict = {}
    today = datetime.date.today()
    maturing_30 = 0
    maturing_90 = 0

    for f in facilities:
        u = calculate_utilization(f)
        total_limit += u.committed_limit
        total_drawn += u.drawn_amount
        total_undrawn += u.undrawn_amount
        total_available += u.available_amount
        ccy = by_currency.setdefault(f.currency_code, {"committed_limit": Decimal(0), "drawn": Decimal(0)})
        ccy["committed_limit"] += u.committed_limit
        ccy["drawn"] += u.drawn_amount
        days_to_maturity = (f.maturity_date - today).days
        if 0 <= days_to_maturity <= 30:
            maturing_30 += 1
        if 0 <= days_to_maturity <= 90:
            maturing_90 += 1

    facility_ids = [f.id for f in facilities]
    covenant_warnings = covenant_breaches = 0
    if facility_ids:
        cov_result = await db.execute(
            select(FacilityCovenant.status).where(FacilityCovenant.facility_id.in_(facility_ids))
        )
        for (cov_status,) in cov_result.all():
            if cov_status == CovenantStatus.WARNING:
                covenant_warnings += 1
            elif cov_status == CovenantStatus.BREACH:
                covenant_breaches += 1

    utilization_pct = (total_drawn / total_limit * 100) if total_limit > 0 else Decimal(0)

    return FundingDashboardOut(
        total_committed_limit=total_limit, total_drawn=total_drawn, total_undrawn=total_undrawn,
        total_available=total_available, utilization_pct=utilization_pct.quantize(Decimal("0.01")),
        facility_count=len(facilities), by_currency=by_currency,
        facilities_maturing_30_days=maturing_30, facilities_maturing_90_days=maturing_90,
        covenant_warnings=covenant_warnings, covenant_breaches=covenant_breaches,
    )


@router.get("/calendar", response_model=list[FundingCalendarEntry])
async def funding_calendar(
    legal_entity_id: uuid.UUID | None = None,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    start_date = start_date or datetime.date.today()
    end_date = end_date or (start_date + datetime.timedelta(days=90))

    entries: list = []

    repay_stmt = (
        select(FacilityRepayment, Facility)
        .join(Facility, Facility.id == FacilityRepayment.facility_id)
        .where(FacilityRepayment.due_date >= start_date, FacilityRepayment.due_date <= end_date)
    )
    if entity_ids != "ALL":
        repay_stmt = repay_stmt.where(FacilityRepayment.legal_entity_id.in_(entity_ids))
    for repayment, facility in (await db.execute(repay_stmt)).all():
        entries.append(FundingCalendarEntry(
            date=repayment.due_date, entity_id=repayment.legal_entity_id, facility_id=facility.id,
            facility_name=facility.facility_name, currency_code=repayment.currency_code,
            event_type=f"{repayment.repayment_type.value}_REPAYMENT",
            amount=repayment.original_amount - repayment.paid_amount, status=repayment.status.value,
        ))

    fee_stmt = (
        select(FacilityFee, Facility)
        .join(Facility, Facility.id == FacilityFee.facility_id)
        .where(FacilityFee.due_date >= start_date, FacilityFee.due_date <= end_date)
    )
    if entity_ids != "ALL":
        fee_stmt = fee_stmt.where(FacilityFee.legal_entity_id.in_(entity_ids))
    for fee, facility in (await db.execute(fee_stmt)).all():
        entries.append(FundingCalendarEntry(
            date=fee.due_date, entity_id=fee.legal_entity_id, facility_id=facility.id,
            facility_name=facility.facility_name, currency_code=fee.currency_code,
            event_type="FEE", amount=fee.amount, status=fee.status.value,
        ))

    maturity_stmt = select(Facility).where(
        Facility.maturity_date >= start_date, Facility.maturity_date <= end_date,
        Facility.is_active.is_(True),
    )
    if entity_ids != "ALL":
        maturity_stmt = maturity_stmt.where(Facility.legal_entity_id.in_(entity_ids))
    for facility in (await db.execute(maturity_stmt)).scalars().all():
        entries.append(FundingCalendarEntry(
            date=facility.maturity_date, entity_id=facility.legal_entity_id, facility_id=facility.id,
            facility_name=facility.facility_name, currency_code=facility.currency_code,
            event_type="MATURITY", amount=facility.committed_limit, status=facility.status.value,
        ))

    entries.sort(key=lambda e: e.date)
    return entries


@router.get("/exposure")
async def funding_exposure(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """SECTION 34: exposure by entity, currency, lender, type, commitment status, maturity bucket."""
    from app.services.funding_capacity_service import facilities_by_maturity_bucket

    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    stmt = select(Facility).where(Facility.is_active.is_(True))
    if entity_ids != "ALL":
        stmt = stmt.where(Facility.legal_entity_id.in_(entity_ids))
    facilities = list((await db.execute(stmt)).scalars().all())

    by_currency: dict = {}
    by_lender: dict = {}
    by_type: dict = {}
    by_commitment: dict = {"COMMITTED": Decimal(0), "UNCOMMITTED": Decimal(0)}
    for f in facilities:
        by_currency[f.currency_code] = by_currency.get(f.currency_code, Decimal(0)) + f.committed_limit
        by_lender[str(f.lender_id)] = by_lender.get(str(f.lender_id), Decimal(0)) + f.committed_limit
        by_type[f.facility_type_code] = by_type.get(f.facility_type_code, Decimal(0)) + f.committed_limit
        by_commitment[f.commitment_type.value] += f.committed_limit

    buckets = await facilities_by_maturity_bucket(db, entity_ids)

    return {
        "by_currency": by_currency, "by_lender": by_lender, "by_type": by_type,
        "by_commitment_type": by_commitment,
        "by_maturity_bucket": [
            {"bucket": b.bucket, "facility_count": b.facility_count,
             "total_committed_limit": b.total_committed_limit}
            for b in buckets
        ],
    }


@router.get("/cost-analysis", response_model=list[FundingCostReportRow])
async def funding_cost_analysis(
    legal_entity_id: uuid.UUID | None = None,
    period_days: int = 30,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    """SECTION 33: cost never defined as interest alone (SECTION 13)."""
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    stmt = select(Facility).where(Facility.is_active.is_(True), Facility.status == FacilityStatus.ACTIVE)
    if entity_ids != "ALL":
        stmt = stmt.where(Facility.legal_entity_id.in_(entity_ids))
    facilities = list((await db.execute(stmt)).scalars().all())

    rows = []
    for f in facilities:
        result = calculate_funding_cost(f, f.current_drawn_amount, period_days)
        rows.append(FundingCostReportRow(
            facility_id=f.id, facility_name=f.facility_name, currency_code=f.currency_code,
            average_drawn=result.average_drawn, interest=result.interest,
            fees=result.commitment_fee + result.arrangement_fee + result.processing_fee,
            total_cost=result.total_cost, effective_cost_pct=result.effective_cost_pct,
        ))
    return rows


@router.get("/gaps", response_model=list[FundingGapOut])
async def funding_gaps(
    forecast_id: uuid.UUID,
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    """SECTION 25: analytical output only - never triggers a drawdown."""
    from app.models.forecast import Forecast

    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    forecast = await db.get(Forecast, forecast_id)
    if forecast is None:
        raise HTTPException(status_code=404, detail="Forecast not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, forecast.legal_entity_id)

    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Forecast).options(selectinload(Forecast.weeks)).where(Forecast.id == forecast_id)
    )
    forecast = result.scalar_one()
    weeks = sorted(forecast.weeks, key=lambda w: w.week_number)

    gaps = await calculate_funding_gaps(db, weeks, entity_ids)
    return [
        FundingGapOut(
            week_number=g.week_number, projected_closing_cash=g.projected_closing_cash,
            minimum_required_liquidity=g.minimum_required_liquidity, cash_shortfall=g.cash_shortfall,
            committed_capacity_applied=g.committed_capacity_applied,
            remaining_unfunded_gap=g.remaining_unfunded_gap,
        )
        for g in gaps
    ]


@router.get("/capacity", response_model=FundingCapacityOut)
async def funding_capacity(
    legal_entity_id: uuid.UUID | None = None,
    currency_code: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingCapacityOut:
    """SECTION 24: cash, committed capacity, and uncommitted capacity kept as three distinct numbers."""
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    capacity = await calculate_funding_capacity(db, entity_ids, currency_code)
    return FundingCapacityOut(
        committed_available=capacity.committed_available,
        uncommitted_potential=capacity.uncommitted_potential,
        total_potential_funding=capacity.total_potential_funding,
        facility_count=capacity.facility_count, by_currency=capacity.by_currency,
    )


@router.get("/recommendations", response_model=list[FundingRecommendationRow])
async def funding_recommendations(
    legal_entity_id: uuid.UUID,
    required_amount: Decimal,
    currency_code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    """
    SECTION 26: transparent comparison factors, never a "best facility"
    ranking - Treasury makes the decision.
    """
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, legal_entity_id)

    stmt = select(Facility).where(
        Facility.is_active.is_(True), Facility.legal_entity_id == legal_entity_id,
        Facility.currency_code == currency_code.upper(),
    )
    facilities = list((await db.execute(stmt)).scalars().all())

    rows = []
    for f in facilities:
        u = calculate_utilization(f)
        reasons = []
        if f.status != FacilityStatus.ACTIVE:
            reasons.append(f"Facility status is {f.status.value}, not ACTIVE.")
        available = u.undrawn_amount if f.commitment_type.value == "UNCOMMITTED" else u.available_amount
        if required_amount > available:
            reasons.append(f"Available capacity {available} is less than required {required_amount}.")
        cost_result = calculate_funding_cost(f, required_amount, 30)
        rows.append(FundingRecommendationRow(
            facility_id=f.id, facility_name=f.facility_name, currency_code=f.currency_code,
            available_amount=available, estimated_cost_pct=cost_result.effective_cost_pct,
            maturity_date=f.maturity_date, commitment_type=f.commitment_type,
            eligible=not reasons, ineligible_reasons=reasons,
        ))
    return rows


@router.get("/forecast-impact")
async def funding_forecast_impact(
    forecast_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """SECTION 38: shows which forecast lines were driven by facility events."""
    from app.models.forecast import Forecast, ForecastLine

    forecast = await db.get(Forecast, forecast_id)
    if forecast is None:
        raise HTTPException(status_code=404, detail="Forecast not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, forecast.legal_entity_id)

    facility_source_types = ["FACILITY_DRAWDOWN", "FACILITY_REPAYMENT", "FACILITY_INTEREST", "FACILITY_FEE"]
    result = await db.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast_id,
            ForecastLine.source_type.in_(facility_source_types),
        )
    )
    lines = list(result.scalars().all())
    return {
        "facility_driven_line_count": len(lines),
        "total_facility_inflows": sum(
            (ln.reporting_amount for ln in lines if ln.direction.value == "INFLOW"), Decimal(0)
        ),
        "total_facility_outflows": sum(
            (ln.reporting_amount for ln in lines if ln.direction.value == "OUTFLOW"), Decimal(0)
        ),
        "lines": [
            {"week_id": str(ln.week_id), "category_code": ln.category_code,
             "direction": ln.direction.value, "amount": str(ln.reporting_amount),
             "source_type": ln.source_type.value, "source_id": ln.source_id,
             "description": ln.description}
            for ln in lines
        ],
    }


@router.post("/actions", response_model=FundingActionOut, status_code=201)
async def create_funding_action(
    payload: FundingActionCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    """SECTION 27: a proposed action, never an automatic execution."""
    await assert_entity_access(db, user, MODULE, TreasuryAction.CREATE, payload.legal_entity_id)

    reasons = await validate_funding_action(
        db, payload.action_type, payload.legal_entity_id, payload.facility_id,
        payload.linked_drawdown_id, payload.linked_repayment_id, payload.amount,
        payload.currency_code,
    )
    if reasons:
        raise HTTPException(status_code=400, detail={"validation_errors": reasons})

    action = FundingAction(**payload.model_dump(), created_by_user_id=user.id)
    db.add(action)
    await db.flush()
    await record_audit_event(
        db, module=MODULE.value, action="FUNDING_ACTION_CREATE", record_type="FundingAction",
        record_id=str(action.id), user_id=user.id, legal_entity_id=action.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(action)
    return action


@router.get("/actions", response_model=list[FundingActionOut])
async def list_funding_actions(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    entity_ids = await _authorized_entity_ids(db, user, legal_entity_id)
    stmt = select(FundingAction)
    if entity_ids != "ALL":
        stmt = stmt.where(FundingAction.legal_entity_id.in_(entity_ids))
    if legal_entity_id:
        stmt = stmt.where(FundingAction.legal_entity_id == legal_entity_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/actions/{action_id}", response_model=FundingActionOut)
async def get_funding_action(
    action_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    action = await db.get(FundingAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Funding action not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, action.legal_entity_id)
    return action


async def _transition_funding_action(
    db: AsyncSession, user: User, action_id: uuid.UUID, new_status: FundingActionStatus,
    required_permission, audit_action: str,
) -> FundingAction:
    """
    SECTION 27/28: enforces the explicit transition table (never an
    arbitrary jump) and the caller's permission for that step - a role
    granted CREATE/SUBMIT need not also hold APPROVE, so segregation of
    duties is whatever the existing permission configuration says it is,
    never assumed or hard-coded here.
    """
    action = await db.get(FundingAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Funding action not found")
    await assert_entity_access(db, user, MODULE, required_permission, action.legal_entity_id)

    allowed = FUNDING_ACTION_STATUS_TRANSITIONS.get(action.status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition funding action from {action.status.value} to "
                   f"{new_status.value}. Allowed: {[s.value for s in allowed] or 'none'}.",
        )

    if new_status == FundingActionStatus.EXECUTED:
        # SECTION 1: never let a FundingAction claim EXECUTED unless the
        # specific underlying transaction it's linked to has genuinely
        # been executed/paid. An action with no link (a general funding
        # decision not tied to one specific drawdown/repayment) has
        # nothing to check against and is allowed through.
        if action.linked_drawdown_id is not None:
            drawdown = await db.get(FacilityDrawdown, action.linked_drawdown_id)
            if drawdown is None or drawdown.status != DrawdownStatus.EXECUTED:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot mark this funding action EXECUTED - its linked drawdown has "
                           "not itself been executed yet. Execute the drawdown first via "
                           "POST /drawdowns/{id}/execute.",
                )
        if action.linked_repayment_id is not None:
            repayment = await db.get(FacilityRepayment, action.linked_repayment_id)
            # SECTION 8 (Stage 3 final hardening): a repayment-linked
            # action requires the repayment to be fully PAID, not merely
            # PARTIALLY_PAID - a documented, deliberate choice. Allowing
            # PARTIALLY_PAID to count as "executed" would let the
            # workflow claim completion while money is still outstanding,
            # which is exactly the ambiguity SECTION 8 asks to avoid.
            if repayment is None or repayment.status != RepaymentStatus.PAID:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot mark this funding action EXECUTED - its linked repayment has "
                           "not been paid IN FULL yet (partial payment does not qualify). "
                           "Record the remaining payment first via POST /repayments/{id}/pay.",
                )

    previous_status = action.status
    action.status = new_status
    if new_status == FundingActionStatus.APPROVED:
        action.approved_by_user_id = user.id
    elif new_status == FundingActionStatus.EXECUTED:
        action.executed_by_user_id = user.id

    await record_audit_event(
        db, module=MODULE.value, action=audit_action, record_type="FundingAction",
        record_id=str(action.id), user_id=user.id, legal_entity_id=action.legal_entity_id,
        previous_value={"status": previous_status.value}, new_value={"status": new_status.value},
    )
    await db.commit()
    await db.refresh(action)
    return action


@router.post("/actions/{action_id}/submit", response_model=FundingActionOut)
async def submit_funding_action(
    action_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    """DRAFT -> SUBMITTED. The proposer puts their draft forward for review."""
    return await _transition_funding_action(
        db, user, action_id, FundingActionStatus.SUBMITTED, TreasuryAction.SUBMIT,
        "FUNDING_ACTION_SUBMIT",
    )


@router.post("/actions/{action_id}/review", response_model=FundingActionOut)
async def review_funding_action(
    action_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    """SUBMITTED -> UNDER_REVIEW. A reviewer picks up a submitted action."""
    return await _transition_funding_action(
        db, user, action_id, FundingActionStatus.UNDER_REVIEW, TreasuryAction.INVESTIGATE,
        "FUNDING_ACTION_REVIEW",
    )


@router.post("/actions/{action_id}/approve", response_model=FundingActionOut)
async def approve_funding_action(
    action_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    """SUBMITTED or UNDER_REVIEW -> APPROVED."""
    return await _transition_funding_action(
        db, user, action_id, FundingActionStatus.APPROVED, TreasuryAction.APPROVE,
        "FUNDING_ACTION_APPROVE",
    )


@router.post("/actions/{action_id}/reject", response_model=FundingActionOut)
async def reject_funding_action(
    action_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    """SUBMITTED or UNDER_REVIEW -> REJECTED. A terminal state - no execution follows."""
    return await _transition_funding_action(
        db, user, action_id, FundingActionStatus.REJECTED, TreasuryAction.APPROVE,
        "FUNDING_ACTION_REJECT",
    )


@router.post("/actions/{action_id}/execute", response_model=FundingActionOut)
async def execute_funding_action(
    action_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    """
    APPROVED -> EXECUTED. SECTION 47: this still does not perform an
    actual bank transaction by itself - it marks the treasury decision as
    executed. Where the proposed action names a specific facility action
    (a drawdown/repayment/etc.), that underlying record is created and
    executed separately through its own endpoint, using its own
    validation - this endpoint only advances the FundingAction's own
    workflow status.
    """
    return await _transition_funding_action(
        db, user, action_id, FundingActionStatus.EXECUTED, TreasuryAction.EXECUTE,
        "FUNDING_ACTION_EXECUTE",
    )


@router.post("/actions/{action_id}/complete", response_model=FundingActionOut)
async def complete_funding_action(
    action_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    """EXECUTED -> COMPLETED. Closes out the workflow record."""
    return await _transition_funding_action(
        db, user, action_id, FundingActionStatus.COMPLETED, TreasuryAction.CLOSE,
        "FUNDING_ACTION_COMPLETE",
    )


@router.post("/actions/{action_id}/cancel", response_model=FundingActionOut)
async def cancel_funding_action(
    action_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FundingAction:
    """DRAFT, SUBMITTED, UNDER_REVIEW, or APPROVED -> CANCELLED. Never once EXECUTED."""
    return await _transition_funding_action(
        db, user, action_id, FundingActionStatus.CANCELLED, TreasuryAction.EDIT,
        "FUNDING_ACTION_CANCEL",
    )
