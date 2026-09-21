import datetime
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.currency import FXRate, FXRateType
from app.models.expected_cash_flow import ExpectedCollection, ExpectedPayment
from app.models.forecast import (
    Forecast,
    ForecastScenarioType,
    ForecastStatus,
    ForecastValueBasis,
)
from app.models.lookup import CashDirection
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.services.forecast_engine import calculate_forecast
from app.services.forecast_views_service import get_currency_view


async def _make_base_forecast(
    db_session, group_id, entity_id=None, reporting_currency="NGN",
    value_basis=ForecastValueBasis.PROBABILITY_ADJUSTED, start=datetime.date(2026, 6, 1),
):
    forecast = Forecast(
        group_id=group_id, legal_entity_id=entity_id, forecast_start_date=start,
        forecast_end_date=start + datetime.timedelta(days=13 * 7 - 1),
        scenario=ForecastScenarioType.BASE, value_basis=value_basis,
        reporting_currency_code=reporting_currency, status=ForecastStatus.DRAFT,
    )
    db_session.add(forecast)
    await db_session.flush()
    return forecast


async def test_opening_cash_pulled_from_bank_balance(
    db_session: AsyncSession, demo_bank_and_account, forecast_categories,
):
    from app.models.balance import BankBalance

    bank, account = demo_bank_and_account
    db_session.add(BankBalance(
        bank_account_id=account.id, balance_date=datetime.date(2026, 5, 31),
        currency_code="NGN", closing_balance=Decimal(1000000), source="MANUAL",
    ))
    await db_session.flush()

    forecast = await _make_base_forecast(
        db_session, group_id=None, entity_id=account.legal_entity_id,
    )
    forecast = await calculate_forecast(db_session, forecast)
    weeks = sorted(forecast.weeks, key=lambda w: w.week_number)
    assert weeks[0].opening_cash == Decimal("1000000.00")


async def test_no_bank_balance_gives_zero_opening_cash_with_warning(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    forecast = await _make_base_forecast(db_session, group_id=group.id, entity_id=entity_a.id)
    forecast = await calculate_forecast(db_session, forecast)
    weeks = sorted(forecast.weeks, key=lambda w: w.week_number)
    assert weeks[0].opening_cash == Decimal("0.00")
    assert any("No bank balance found" in w for w in forecast.data_quality_warnings)


async def test_weekly_waterfall_gross_basis(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    db_session.add(ExpectedCollection(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="NGN", amount=Decimal(100000), category="CUSTOMER_COLLECTIONS",
        probability=80,
    ))
    db_session.add(ExpectedPayment(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 4),
        currency_code="NGN", amount=Decimal(30000), category="SUPPLIER_PAYMENTS",
    ))
    await db_session.flush()

    forecast = await _make_base_forecast(
        db_session, group_id=group.id, entity_id=entity_a.id,
        value_basis=ForecastValueBasis.GROSS,
    )
    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)

    assert week1.total_inflows == Decimal("100000.00")
    assert week1.total_outflows == Decimal("30000.00")
    assert week1.net_cash_flow == Decimal("70000.00")
    assert week1.closing_cash == week1.opening_cash + Decimal("70000.00")


async def test_probability_adjusted_basis_scales_expected_collection(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    db_session.add(ExpectedCollection(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="NGN", amount=Decimal(100000), category="CUSTOMER_COLLECTIONS",
        probability=80,
    ))
    await db_session.flush()

    forecast = await _make_base_forecast(
        db_session, group_id=group.id, entity_id=entity_a.id,
        value_basis=ForecastValueBasis.PROBABILITY_ADJUSTED,
    )
    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)
    assert week1.total_inflows == Decimal("80000.00")


async def test_multi_entity_group_forecast_aggregates_both_entities(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, entity_b = demo_group_and_entities
    db_session.add(ExpectedCollection(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="NGN", amount=Decimal(50000), category="CUSTOMER_COLLECTIONS",
    ))
    db_session.add(ExpectedCollection(
        legal_entity_id=entity_b.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="USD", amount=Decimal(100), category="CUSTOMER_COLLECTIONS",
    ))
    db_session.add(FXRate(
        from_currency_code="USD", to_currency_code="NGN", rate_type=FXRateType.SPOT,
        rate_date=datetime.date(2026, 5, 1), rate=Decimal(1500),
    ))
    await db_session.flush()

    forecast = await _make_base_forecast(
        db_session, group_id=group.id, entity_id=None, value_basis=ForecastValueBasis.GROSS,
    )
    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)
    assert week1.total_inflows == Decimal("200000.00")  # 50000 + 100*1500


async def test_currency_view_never_hides_a_shortfall(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, entity_b = demo_group_and_entities
    db_session.add(ExpectedCollection(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="NGN", amount=Decimal(10000000), category="CUSTOMER_COLLECTIONS",
    ))
    db_session.add(ExpectedPayment(
        legal_entity_id=entity_b.id, expected_date=datetime.date(2026, 6, 3),
        currency_code="USD", amount=Decimal(500), category="SUPPLIER_PAYMENTS",
    ))
    db_session.add(FXRate(
        from_currency_code="USD", to_currency_code="NGN", rate_type=FXRateType.SPOT,
        rate_date=datetime.date(2026, 5, 1), rate=Decimal(1500),
    ))
    await db_session.flush()

    forecast = await _make_base_forecast(
        db_session, group_id=group.id, entity_id=None, value_basis=ForecastValueBasis.GROSS,
    )
    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)
    assert week1.closing_cash > 0

    usd_view = await get_currency_view(db_session, forecast, "USD")
    assert usd_view[0]["closing_cash"] == Decimal(-500)


async def test_transfer_between_entities_nets_to_zero_at_group_level(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, entity_b = demo_group_and_entities
    pair_id = uuid.uuid4()
    db_session.add(TreasuryTransaction(
        legal_entity_id=entity_b.id, event_type_code="INTERCOMPANY_PAYMENT",
        direction=CashDirection.TRANSFER, event_date=datetime.date(2026, 6, 2),
        transaction_currency_code="USD", transaction_amount=Decimal(1000),
        transfer_pair_id=pair_id, status=TransactionStatus.POSTED,
    ))
    db_session.add(TreasuryTransaction(
        legal_entity_id=entity_a.id, event_type_code="INTERCOMPANY_RECEIPT",
        direction=CashDirection.TRANSFER, event_date=datetime.date(2026, 6, 2),
        transaction_currency_code="USD", transaction_amount=Decimal(1000),
        transfer_pair_id=pair_id, status=TransactionStatus.POSTED,
    ))
    await db_session.flush()

    forecast = await _make_base_forecast(
        db_session, group_id=group.id, entity_id=None, reporting_currency="USD",
        value_basis=ForecastValueBasis.GROSS,
    )
    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)
    assert week1.net_transfers == Decimal("0.00")


async def test_recalculating_a_published_forecast_is_rejected(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    forecast = await _make_base_forecast(db_session, group_id=group.id, entity_id=entity_a.id)
    forecast = await calculate_forecast(db_session, forecast)
    forecast.status = ForecastStatus.PUBLISHED
    await db_session.flush()

    with pytest.raises(ValueError, match="published"):
        await calculate_forecast(db_session, forecast)
