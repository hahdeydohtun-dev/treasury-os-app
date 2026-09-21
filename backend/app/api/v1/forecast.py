import datetime
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.authorization import (
    apply_entity_scope,
    assert_entity_access,
    assert_record_belongs_to_authorized_entity,
    get_authorized_scope,
)
from app.auth.dependencies import get_current_user, require_permission
from app.db.session import get_db
from app.models.entity import LegalEntity
from app.models.forecast import (
    AlertStatus,
    Forecast,
    ForecastAdjustment,
    ForecastAlert,
    ForecastCategory,
    ForecastScenarioAssumption,
    ForecastStatus,
    LiquidityThreshold,
    RecurringCashFlow,
)
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.forecast import (
    AccuracyOut,
    CurrencyViewRow,
    EntityViewRow,
    ForecastAdjustmentCreate,
    ForecastAdjustmentOut,
    ForecastAlertOut,
    ForecastCategoryCreate,
    ForecastCategoryOut,
    ForecastCreate,
    ForecastLineOut,
    ForecastOut,
    ForecastSummaryOut,
    ForecastWeekOut,
    LiquidityThresholdCreate,
    LiquidityThresholdOut,
    RecurringCashFlowCreate,
    RecurringCashFlowOut,
    ScenarioAssumptionCreate,
    ScenarioAssumptionOut,
    VarianceRowOut,
    WhatIfComparisonWeek,
    WhatIfRequest,
    WhatIfResult,
)
from app.services.audit_service import record_audit_event
from app.services.forecast_dates import FORECAST_HORIZON_WEEKS
from app.services.forecast_engine import calculate_forecast
from app.services.forecast_export_service import build_forecast_export
from app.services.forecast_lifecycle_service import (
    archive_forecast,
    publish_forecast,
    roll_forecast,
)
from app.services.forecast_variance_service import compute_accuracy, compute_variance
from app.services.forecast_views_service import get_currency_view, get_entity_view

router = APIRouter(prefix="/forecast", tags=["forecast"])

FORECAST = TreasuryModule.FORECAST_13WK
DEFAULT_TARGET_VARIANCE_PCT = Decimal(5)


async def _load_forecast(db: AsyncSession, forecast_id: uuid.UUID) -> Forecast:
    result = await db.execute(
        select(Forecast)
        .options(selectinload(Forecast.weeks), selectinload(Forecast.assumptions))
        .where(Forecast.id == forecast_id)
    )
    forecast = result.scalar_one_or_none()
    if forecast is None:
        raise HTTPException(status_code=404, detail="Forecast not found")
    return forecast


async def _assert_forecast_scope(
    db: AsyncSession, user: User, forecast: Forecast, action: TreasuryAction
) -> None:
    """
    Verifies the CALLER is authorized for THIS forecast's own entity/group
    - never derived from a request query parameter, always from the
      record itself, so changing the URL/query string cannot widen access.
    """
    await assert_record_belongs_to_authorized_entity(
        db, user, FORECAST, action, forecast.legal_entity_id, forecast.group_id,
    )


@router.get("/admin/categories", response_model=list[ForecastCategoryOut])
async def list_categories(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(select(ForecastCategory).where(ForecastCategory.is_active.is_(True)))
    return list(result.scalars().all())


@router.post(
    "/admin/categories", response_model=ForecastCategoryOut, status_code=status.HTTP_201_CREATED
)
async def create_category(
    payload: ForecastCategoryCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(FORECAST, TreasuryAction.CONFIGURE)),
) -> ForecastCategory:
    category = ForecastCategory(**payload.model_dump())
    db.add(category)
    await db.flush()
    await record_audit_event(
        db, module=FORECAST.value, action="CATEGORY_CREATE", record_type="ForecastCategory",
        record_id=category.code, user_id=user.id, new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(category)
    return category


@router.get("/admin/recurring-cash-flows", response_model=list[RecurringCashFlowOut])
async def list_recurring_flows(
    legal_entity_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    scope = await get_authorized_scope(db, user, FORECAST, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail=f"No {FORECAST.value}:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    stmt = select(RecurringCashFlow).where(RecurringCashFlow.is_active.is_(True))
    stmt = await apply_entity_scope(db, stmt, RecurringCashFlow.legal_entity_id, scope)
    if legal_entity_id:
        stmt = stmt.where(RecurringCashFlow.legal_entity_id == legal_entity_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post(
    "/admin/recurring-cash-flows", response_model=RecurringCashFlowOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_recurring_flow(
    payload: RecurringCashFlowCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RecurringCashFlow:
    await assert_entity_access(db, user, FORECAST, TreasuryAction.CONFIGURE, payload.legal_entity_id)
    flow = RecurringCashFlow(**payload.model_dump())
    db.add(flow)
    await db.flush()
    await record_audit_event(
        db, module=FORECAST.value, action="RECURRING_FLOW_CREATE", record_type="RecurringCashFlow",
        record_id=str(flow.id), user_id=user.id, legal_entity_id=flow.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(flow)
    return flow


@router.get("/admin/liquidity-thresholds", response_model=list[LiquidityThresholdOut])
async def list_liquidity_thresholds(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    scope = await get_authorized_scope(db, user, FORECAST, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail=f"No {FORECAST.value}:VIEW permission.")

    stmt = select(LiquidityThreshold).where(LiquidityThreshold.is_active.is_(True))
    if not scope.unrestricted:
        from sqlalchemy import false, or_

        conditions = []
        if scope.entity_ids:
            conditions.append(LiquidityThreshold.legal_entity_id.in_(scope.entity_ids))
        if scope.group_ids:
            # GROUP/CURRENCY-scope thresholds (no legal_entity_id) are
            # administrative config, visible only to group-wide users.
            conditions.append(LiquidityThreshold.legal_entity_id.is_(None))
        stmt = stmt.where(or_(*conditions)) if conditions else stmt.where(false())
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post(
    "/admin/liquidity-thresholds", response_model=LiquidityThresholdOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_liquidity_threshold(
    payload: LiquidityThresholdCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> LiquidityThreshold:
    await assert_entity_access(db, user, FORECAST, TreasuryAction.CONFIGURE, payload.legal_entity_id)
    threshold = LiquidityThreshold(**payload.model_dump())
    db.add(threshold)
    await db.flush()
    await record_audit_event(
        db, module=FORECAST.value, action="LIQUIDITY_THRESHOLD_CREATE",
        record_type="LiquidityThreshold", record_id=str(threshold.id), user_id=user.id,
        legal_entity_id=threshold.legal_entity_id, new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(threshold)
    return threshold


@router.post("", response_model=ForecastOut, status_code=status.HTTP_201_CREATED)
async def create_forecast(
    payload: ForecastCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Forecast:
    if payload.legal_entity_id is not None:
        entity = await assert_entity_access(
            db, user, FORECAST, TreasuryAction.CREATE, payload.legal_entity_id,
        )
        if payload.group_id is not None and entity.group_id != payload.group_id:
            raise HTTPException(
                status_code=400,
                detail="legal_entity_id does not belong to the specified group_id.",
            )
    elif payload.group_id is not None:
        scope = await get_authorized_scope(db, user, FORECAST, TreasuryAction.CREATE)
        if not scope.allows_group(payload.group_id):
            raise HTTPException(
                status_code=403, detail="No group-wide permission to create a forecast for this group.",
            )
    else:
        raise HTTPException(
            status_code=400, detail="Either legal_entity_id or group_id must be provided.",
        )

    end_date = payload.forecast_start_date + datetime.timedelta(days=7 * FORECAST_HORIZON_WEEKS - 1)
    forecast = Forecast(
        **payload.model_dump(), forecast_end_date=end_date, created_by_user_id=user.id,
    )
    db.add(forecast)
    await db.flush()
    await record_audit_event(
        db, module=FORECAST.value, action="CREATE", record_type="Forecast",
        record_id=str(forecast.id), user_id=user.id, legal_entity_id=forecast.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(forecast)
    return forecast


@router.get("", response_model=list[ForecastOut])
async def list_forecasts(
    legal_entity_id: uuid.UUID | None = None,
    status_filter: ForecastStatus | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    """
    Server-side, query-level entity scoping (Stage 2 Hardening): the
    authorized scope is resolved from the user's role assignments and
    applied as a WHERE clause before any rows are fetched - never by
    fetching everything and filtering in Python, and never by gating the
    whole request behind a single query-param entity_id.

    - Entity-scoped user, no filter -> only their authorized entities'
      forecasts (including no rows if they have none), never a blanket
      403 and never another entity's data.
    - Entity-scoped user, `legal_entity_id` = an entity they're NOT
      authorized for -> 403 (the query-string entity_id is validated
      against their scope, not trusted blindly).
    - Group-wide user -> their authorized group(s)' entities, plus any
      group-wide (consolidated) forecasts belonging to those same groups.
    """
    scope = await get_authorized_scope(db, user, FORECAST, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail=f"No {FORECAST.value}:VIEW permission.")

    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    stmt = select(Forecast).order_by(Forecast.created_at.desc())
    stmt = await apply_entity_scope(db, stmt, Forecast.legal_entity_id, scope, Forecast.group_id)
    if legal_entity_id:
        stmt = stmt.where(Forecast.legal_entity_id == legal_entity_id)
    if status_filter:
        stmt = stmt.where(Forecast.status == status_filter)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{forecast_id}", response_model=ForecastOut)
async def get_forecast(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Forecast:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    return forecast


@router.post("/{forecast_id}/calculate", response_model=ForecastOut)
async def calculate(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Forecast:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.EDIT)
    try:
        forecast = await calculate_forecast(db, forecast)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await record_audit_event(
        db, module=FORECAST.value, action="CALCULATE", record_type="Forecast",
        record_id=str(forecast.id), user_id=user.id, legal_entity_id=forecast.legal_entity_id,
        new_value={"warnings": forecast.data_quality_warnings},
    )
    await db.commit()
    await db.refresh(forecast)
    return forecast


@router.post("/{forecast_id}/publish", response_model=ForecastOut)
async def publish(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Forecast:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.APPROVE)
    try:
        forecast = await publish_forecast(db, forecast, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await record_audit_event(
        db, module=FORECAST.value, action="PUBLISH", record_type="Forecast",
        record_id=str(forecast.id), user_id=user.id, legal_entity_id=forecast.legal_entity_id,
    )
    await db.commit()
    await db.refresh(forecast)
    return forecast


@router.post("/{forecast_id}/archive", response_model=ForecastOut)
async def archive(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Forecast:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.EDIT)
    forecast = await archive_forecast(db, forecast)
    await record_audit_event(
        db, module=FORECAST.value, action="ARCHIVE", record_type="Forecast",
        record_id=str(forecast.id), user_id=user.id, legal_entity_id=forecast.legal_entity_id,
    )
    await db.commit()
    await db.refresh(forecast)
    return forecast


@router.post("/{forecast_id}/roll", response_model=ForecastOut, status_code=status.HTTP_201_CREATED)
async def roll(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Forecast:
    parent = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, parent, TreasuryAction.CREATE)
    new_forecast = await roll_forecast(db, parent, user.id)
    await record_audit_event(
        db, module=FORECAST.value, action="ROLL", record_type="Forecast",
        record_id=str(new_forecast.id), user_id=user.id, legal_entity_id=new_forecast.legal_entity_id,
        previous_value={"parent_forecast_id": str(parent.id)},
    )
    await db.commit()
    await db.refresh(new_forecast)
    return new_forecast


@router.get("/{forecast_id}/weeks", response_model=list[ForecastWeekOut])
async def get_weeks(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    return sorted(forecast.weeks, key=lambda w: w.week_number)


@router.get("/{forecast_id}/summary", response_model=ForecastSummaryOut)
async def get_summary(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ForecastSummaryOut:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    weeks = sorted(forecast.weeks, key=lambda w: w.week_number)
    if not weeks:
        raise HTTPException(status_code=400, detail="Forecast has not been calculated yet.")

    lowest = min(weeks, key=lambda w: w.closing_cash)
    gaps = [w for w in weeks if w.surplus_or_gap < 0]
    largest_gap = min(gaps, key=lambda w: w.surplus_or_gap) if gaps else None
    coverages = [w.liquidity_coverage_ratio for w in weeks if w.liquidity_coverage_ratio is not None]

    return ForecastSummaryOut(
        forecast=ForecastOut.model_validate(forecast),
        weeks=[ForecastWeekOut.model_validate(w) for w in weeks],
        opening_cash=weeks[0].opening_cash,
        thirteen_week_net_cash_flow=sum((w.net_cash_flow for w in weeks), Decimal(0)),
        lowest_projected_cash=lowest.closing_cash,
        lowest_projected_cash_week=lowest.week_number,
        largest_funding_gap=largest_gap.surplus_or_gap if largest_gap else Decimal(0),
        largest_funding_gap_week=largest_gap.week_number if largest_gap else None,
        total_surplus=sum((w.surplus_or_gap for w in weeks if w.surplus_or_gap > 0), Decimal(0)),
        average_liquidity_coverage=(
            (sum(coverages, Decimal(0)) / len(coverages)) if coverages else None
        ),
    )


@router.get("/{forecast_id}/export")
async def export_forecast(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """
    SECTION 48: exports the selected forecast version/scenario to Excel.
    Built directly from ForecastWeek/ForecastLine (the same data every
    other endpoint reads), so the export can never show different numbers
    than the dashboard.
    """
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.EXPORT)
    if not forecast.weeks:
        raise HTTPException(status_code=400, detail="Forecast has not been calculated yet.")

    file_bytes = await build_forecast_export(db, forecast)
    filename = f"forecast_{forecast.forecast_start_date}_{forecast.scenario.value}_v{forecast.version}.xlsx"
    return StreamingResponse(
        iter([file_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{forecast_id}/lines", response_model=list[ForecastLineOut])
async def get_lines(
    forecast_id: uuid.UUID,
    week_number: int | None = None,
    legal_entity_id: uuid.UUID | None = None,
    currency_code: str | None = None,
    category_code: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    from app.models.forecast import ForecastLine

    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)

    stmt = select(ForecastLine).where(ForecastLine.forecast_id == forecast_id)
    if legal_entity_id:
        stmt = stmt.where(ForecastLine.legal_entity_id == legal_entity_id)
    if currency_code:
        stmt = stmt.where(ForecastLine.transaction_currency_code == currency_code.upper())
    if category_code:
        stmt = stmt.where(ForecastLine.category_code == category_code)
    if week_number:
        week = next((w for w in forecast.weeks if w.week_number == week_number), None)
        if week:
            stmt = stmt.where(ForecastLine.week_id == week.id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{forecast_id}/entity-view", response_model=list[EntityViewRow])
async def entity_view(
    forecast_id: uuid.UUID,
    legal_entity_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    await assert_entity_access(db, user, FORECAST, TreasuryAction.VIEW, legal_entity_id)
    # Data integrity: the requested entity must actually be within this
    # forecast's own scope, independent of the caller's broader access -
    # an entity-scoped forecast's entity-view can't be redirected to a
    # different entity just because the caller happens to also have
    # access to that other entity.
    if forecast.legal_entity_id is not None and forecast.legal_entity_id != legal_entity_id:
        raise HTTPException(status_code=400, detail="Entity is not part of this forecast.")
    if forecast.legal_entity_id is None:
        entity = await db.get(LegalEntity, legal_entity_id)
        if entity is None or entity.group_id != forecast.group_id:
            raise HTTPException(status_code=400, detail="Entity is not part of this forecast's group.")
    return await get_entity_view(db, forecast, legal_entity_id)


@router.get("/{forecast_id}/currency-view", response_model=list[CurrencyViewRow])
async def currency_view(
    forecast_id: uuid.UUID,
    currency_code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    return await get_currency_view(db, forecast, currency_code.upper())


@router.get("/{forecast_id}/variance", response_model=list[VarianceRowOut])
async def variance(
    forecast_id: uuid.UUID,
    group_by: str = "week",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    if group_by not in ("week", "entity", "currency", "category", "group"):
        raise HTTPException(status_code=400, detail="Invalid group_by value.")
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    rows = await compute_variance(db, forecast, group_by=group_by)
    return [VarianceRowOut(**vars(r)) for r in rows]


@router.get("/{forecast_id}/accuracy", response_model=AccuracyOut)
async def accuracy(
    forecast_id: uuid.UUID,
    target_variance_pct: Decimal = DEFAULT_TARGET_VARIANCE_PCT,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AccuracyOut:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    result = await compute_accuracy(db, forecast, target_variance_pct)
    return AccuracyOut(**result)


@router.get("/{forecast_id}/alerts", response_model=list[ForecastAlertOut])
async def get_alerts(
    forecast_id: uuid.UUID,
    status_filter: AlertStatus | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    stmt = select(ForecastAlert).where(ForecastAlert.forecast_id == forecast_id)
    if status_filter:
        stmt = stmt.where(ForecastAlert.status == status_filter)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch("/{forecast_id}/alerts/{alert_id}", response_model=ForecastAlertOut)
async def update_alert_status(
    forecast_id: uuid.UUID,
    alert_id: uuid.UUID,
    new_status: AlertStatus,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ForecastAlert:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.ADJUST)
    alert = await db.get(ForecastAlert, alert_id)
    if alert is None or alert.forecast_id != forecast_id:
        raise HTTPException(status_code=404, detail="Alert not found")
    previous = alert.status
    alert.status = new_status
    await record_audit_event(
        db, module=FORECAST.value, action="ALERT_STATUS_CHANGE", record_type="ForecastAlert",
        record_id=str(alert.id), user_id=user.id,
        previous_value={"status": previous.value}, new_value={"status": new_status.value},
    )
    await db.commit()
    await db.refresh(alert)
    return alert


@router.get("/{forecast_id}/funding-gaps", response_model=list[ForecastWeekOut])
async def funding_gaps(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    return sorted(
        (w for w in forecast.weeks if w.surplus_or_gap < 0), key=lambda w: w.week_number
    )


@router.get("/{forecast_id}/surplus", response_model=list[ForecastWeekOut])
async def surplus(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    return sorted(
        (w for w in forecast.weeks if w.surplus_or_gap > 0), key=lambda w: w.week_number
    )


@router.post(
    "/{forecast_id}/adjustments", response_model=ForecastAdjustmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_adjustment(
    forecast_id: uuid.UUID,
    payload: ForecastAdjustmentCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ForecastAdjustment:
    forecast = await _load_forecast(db, forecast_id)
    if forecast.status == ForecastStatus.PUBLISHED:
        raise HTTPException(status_code=400, detail="Cannot adjust a published forecast.")
    entity = await assert_entity_access(
        db, user, FORECAST, TreasuryAction.ADJUST, payload.legal_entity_id,
    )
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.ADJUST)
    # Data integrity (SECTION 23): an adjustment must belong to an entity
    # actually within this forecast's own scope, regardless of the
    # caller's broader access to other entities.
    if forecast.legal_entity_id is not None and forecast.legal_entity_id != payload.legal_entity_id:
        raise HTTPException(status_code=400, detail="Entity is not part of this forecast.")
    if forecast.legal_entity_id is None and entity.group_id != forecast.group_id:
        raise HTTPException(status_code=400, detail="Entity is not part of this forecast's group.")

    adjustment = ForecastAdjustment(
        forecast_id=forecast_id, **payload.model_dump(), created_by_user_id=user.id,
    )
    db.add(adjustment)
    await db.flush()
    await record_audit_event(
        db, module=FORECAST.value, action="ADJUST", record_type="ForecastAdjustment",
        record_id=str(adjustment.id), user_id=user.id, legal_entity_id=adjustment.legal_entity_id,
        reason=payload.reason, new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(adjustment)
    return adjustment


@router.get("/{forecast_id}/adjustments", response_model=list[ForecastAdjustmentOut])
async def list_adjustments(
    forecast_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.VIEW)
    result = await db.execute(
        select(ForecastAdjustment).where(ForecastAdjustment.forecast_id == forecast_id)
    )
    return list(result.scalars().all())


@router.post(
    "/{forecast_id}/scenarios", response_model=ScenarioAssumptionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_scenario_assumption(
    forecast_id: uuid.UUID,
    payload: ScenarioAssumptionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ForecastScenarioAssumption:
    forecast = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, forecast, TreasuryAction.CONFIGURE)
    if forecast.status == ForecastStatus.PUBLISHED:
        raise HTTPException(status_code=400, detail="Cannot change assumptions on a published forecast.")

    assumption = ForecastScenarioAssumption(forecast_id=forecast_id, **payload.model_dump())
    db.add(assumption)
    await db.flush()
    await record_audit_event(
        db, module=FORECAST.value, action="ASSUMPTION_CHANGE", record_type="ForecastScenarioAssumption",
        record_id=str(assumption.id), user_id=user.id, legal_entity_id=forecast.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(assumption)
    return assumption


@router.post("/{forecast_id}/what-if", response_model=WhatIfResult, status_code=status.HTTP_201_CREATED)
async def what_if(
    forecast_id: uuid.UUID,
    payload: WhatIfRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WhatIfResult:
    base = await _load_forecast(db, forecast_id)
    await _assert_forecast_scope(db, user, base, TreasuryAction.VIEW)
    if not base.weeks:
        raise HTTPException(status_code=400, detail="Base forecast has not been calculated yet.")

    scenario_forecast = Forecast(
        group_id=base.group_id, legal_entity_id=base.legal_entity_id,
        forecast_start_date=base.forecast_start_date, forecast_end_date=base.forecast_end_date,
        scenario=base.scenario, value_basis=base.value_basis,
        reporting_currency_code=base.reporting_currency_code,
        version=1, status=ForecastStatus.DRAFT,
        assumptions_notes=f"WHAT-IF (base: {base.id}): {payload.label}",
        created_by_user_id=user.id,
    )
    db.add(scenario_forecast)
    await db.flush()

    for a in (
        await db.execute(
            select(ForecastScenarioAssumption).where(ForecastScenarioAssumption.forecast_id == base.id)
        )
    ).scalars().all():
        db.add(ForecastScenarioAssumption(
            forecast_id=scenario_forecast.id, assumption_type=a.assumption_type,
            numeric_value=a.numeric_value, currency_code=a.currency_code,
            category_code=a.category_code, description=a.description,
        ))
    base_adjustments = (
        await db.execute(select(ForecastAdjustment).where(ForecastAdjustment.forecast_id == base.id))
    ).scalars().all()
    for adj in base_adjustments:
        db.add(ForecastAdjustment(
            forecast_id=scenario_forecast.id, legal_entity_id=adj.legal_entity_id,
            currency_code=adj.currency_code, week_number=adj.week_number,
            category_code=adj.category_code, amount=adj.amount, direction=adj.direction,
            reason=adj.reason, status=adj.status, created_by_user_id=user.id,
        ))
    for adj_payload in payload.adjustments:
        db.add(ForecastAdjustment(
            forecast_id=scenario_forecast.id, **adj_payload.model_dump(),
            created_by_user_id=user.id,
        ))
    await db.flush()

    scenario_forecast = await calculate_forecast(db, scenario_forecast)
    await db.commit()
    await db.refresh(scenario_forecast, attribute_names=["weeks"])

    base_weeks = {w.week_number: w for w in base.weeks}
    comparisons = []
    for w in sorted(scenario_forecast.weeks, key=lambda w: w.week_number):
        base_week = base_weeks.get(w.week_number)
        if base_week is None:
            continue
        comparisons.append(WhatIfComparisonWeek(
            week_number=w.week_number,
            base_closing_cash=base_week.closing_cash, scenario_closing_cash=w.closing_cash,
            closing_cash_difference=w.closing_cash - base_week.closing_cash,
            base_surplus_or_gap=base_week.surplus_or_gap, scenario_surplus_or_gap=w.surplus_or_gap,
            surplus_gap_difference=w.surplus_or_gap - base_week.surplus_or_gap,
        ))

    return WhatIfResult(scenario_forecast_id=scenario_forecast.id, weeks=comparisons)


