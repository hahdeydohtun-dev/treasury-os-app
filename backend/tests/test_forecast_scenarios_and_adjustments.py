import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expected_cash_flow import ExpectedCollection, ExpectedPayment
from app.models.forecast import (
    Forecast,
    ForecastAdjustment,
    ForecastScenarioAssumption,
    ForecastScenarioType,
    ForecastStatus,
    ForecastValueBasis,
    LiquidityScopeType,
    LiquidityThreshold,
    RecurringCashFlow,
    RecurringFrequency,
)
from app.models.lookup import CashDirection
from app.services.forecast_engine import calculate_forecast


async def _make_forecast(db_session, group_id, entity_id, **kwargs):
    kwargs.setdefault("scenario", ForecastScenarioType.BASE)
    kwargs.setdefault("value_basis", ForecastValueBasis.GROSS)
    forecast = Forecast(
        group_id=group_id, legal_entity_id=entity_id,
        forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 6, 1) + datetime.timedelta(days=13 * 7 - 1),
        reporting_currency_code="NGN", status=ForecastStatus.DRAFT, **kwargs,
    )
    db_session.add(forecast)
    await db_session.flush()
    return forecast


async def test_collection_delay_assumption_shifts_week(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    db_session.add(ExpectedCollection(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="NGN", amount=Decimal(100000), category="CUSTOMER_COLLECTIONS",
    ))
    forecast = await _make_forecast(db_session, group.id, entity_a.id)
    db_session.add(ForecastScenarioAssumption(
        forecast_id=forecast.id, assumption_type="COLLECTION_DELAY_WEEKS", numeric_value=Decimal(2),
    ))
    await db_session.flush()

    forecast = await calculate_forecast(db_session, forecast)
    weeks = {w.week_number: w for w in forecast.weeks}
    assert weeks[1].total_inflows == Decimal("0.00")
    assert weeks[3].total_inflows == Decimal("100000.00")


async def test_unexpected_outflow_assumption_adds_stress_line(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    db_session.add(ExpectedCollection(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="NGN", amount=Decimal(100000), category="CUSTOMER_COLLECTIONS",
    ))
    forecast = await _make_forecast(
        db_session, group.id, entity_a.id, scenario=ForecastScenarioType.STRESS,
    )
    db_session.add(ForecastScenarioAssumption(
        forecast_id=forecast.id, assumption_type="UNEXPECTED_OUTFLOW_AMOUNT",
        numeric_value=Decimal(500000), currency_code="NGN", category_code="OTHER_OUTFLOW",
        description="Stress: emergency vendor payment",
    ))
    await db_session.flush()

    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)
    assert week1.total_outflows == Decimal("500000.00")
    assert week1.net_cash_flow == Decimal("100000.00") - Decimal("500000.00")


async def test_min_cash_buffer_multiplier_scales_minimum_liquidity(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    db_session.add(LiquidityThreshold(
        scope_type=LiquidityScopeType.ENTITY, legal_entity_id=entity_a.id,
        minimum_amount=Decimal(100000),
    ))
    forecast = await _make_forecast(db_session, group.id, entity_a.id)
    db_session.add(ForecastScenarioAssumption(
        forecast_id=forecast.id, assumption_type="MIN_CASH_BUFFER_MULTIPLIER",
        numeric_value=Decimal("1.5"),
    ))
    await db_session.flush()

    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)
    assert week1.minimum_required_liquidity == Decimal("150000.00")


async def test_manual_adjustment_applied_as_additive_layer(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    forecast = await _make_forecast(db_session, group.id, entity_a.id)
    db_session.add(ForecastAdjustment(
        forecast_id=forecast.id, legal_entity_id=entity_a.id, currency_code="NGN",
        week_number=5, category_code="OTHER_OUTFLOW", amount=Decimal(50000),
        direction=CashDirection.OUTFLOW, reason="Add unplanned tax payment",
    ))
    await db_session.flush()

    forecast = await calculate_forecast(db_session, forecast)
    week5 = next(w for w in forecast.weeks if w.week_number == 5)
    assert week5.total_outflows == Decimal("50000.00")

    result = await db_session.execute(
        select(ForecastAdjustment).where(ForecastAdjustment.forecast_id == forecast.id)
    )
    assert len(result.scalars().all()) == 1


async def test_recurring_monthly_cash_flow_expands_into_weeks(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    db_session.add(RecurringCashFlow(
        name="Monthly rent", legal_entity_id=entity_a.id, currency_code="NGN",
        category_code="OTHER_OUTFLOW", amount=Decimal(200000), direction=CashDirection.OUTFLOW,
        start_date=datetime.date(2026, 6, 1), frequency=RecurringFrequency.MONTHLY,
    ))
    forecast = await _make_forecast(db_session, group.id, entity_a.id)
    await db_session.flush()

    forecast = await calculate_forecast(db_session, forecast)
    total_rent_outflow = sum((w.total_outflows for w in forecast.weeks), Decimal(0))
    assert total_rent_outflow == Decimal("600000.00")


async def test_expected_payment_without_probability_is_not_scaled_in_probability_mode(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    db_session.add(ExpectedPayment(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="NGN", amount=Decimal(40000), category="SUPPLIER_PAYMENTS",
        probability=None,
    ))
    forecast = await _make_forecast(
        db_session, group.id, entity_a.id, value_basis=ForecastValueBasis.PROBABILITY_ADJUSTED,
    )
    await db_session.flush()

    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)
    assert week1.total_outflows == Decimal("40000.00")
