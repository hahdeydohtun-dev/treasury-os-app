"""
DEMO DATA - Stage 3 extension. Builds on the Stage 2 demo Group/Entities
(see app/db/seed_demo_data.py). Never represent this as real financial
data (SECTION 52).

Adds:
  - Liquidity thresholds for both demo entities
  - Recurring payroll/opex cash flows
  - A spread of expected collections/payments across the 13-week horizon,
    including one deliberately large payment that creates a projected
    liquidity gap and a later large collection that recovers into surplus
  - A calculated (but not published) demo Forecast

Run after app/db/seed_demo_data.py:
    python -m app.db.seed_forecast_demo_data
Safe to run multiple times: skips if a forecast already exists for the
demo group.
"""
import asyncio
import datetime
from decimal import Decimal

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.currency import FXRate, FXRateType
from app.models.entity import Group, LegalEntity
from app.models.expected_cash_flow import ExpectedCollection, ExpectedPayment
from app.models.forecast import (
    Forecast,
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

DEMO_GROUP_CODE = "DEMO"
FORECAST_START = datetime.date(2026, 9, 14)


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        group = (
            await db.execute(select(Group).where(Group.code == DEMO_GROUP_CODE))
        ).scalars().first()
        if group is None:
            print("Run app.db.seed_demo_data first - no DEMO group found.")
            return

        existing_forecast = (
            await db.execute(select(Forecast).where(Forecast.group_id == group.id))
        ).scalars().first()
        if existing_forecast is not None:
            print("Stage 3 demo forecast already present. Skipping.")
            return

        entities = (
            await db.execute(select(LegalEntity).where(LegalEntity.group_id == group.id))
        ).scalars().all()
        entity_a = next(e for e in entities if e.code == "DEMO-NG")
        entity_b = next(e for e in entities if e.code == "DEMO-US")

        existing_rate = (
            await db.execute(
                select(FXRate).where(
                    FXRate.from_currency_code == "USD", FXRate.to_currency_code == "NGN",
                    FXRate.is_current.is_(True),
                )
            )
        ).scalars().first()
        if existing_rate is None:
            db.add(FXRate(
                from_currency_code="USD", to_currency_code="NGN", rate_type=FXRateType.SPOT,
                rate_date=FORECAST_START - datetime.timedelta(days=1), rate=Decimal(1500),
                rate_source="DEMO_DATA",
            ))

        db.add(LiquidityThreshold(
            scope_type=LiquidityScopeType.ENTITY, legal_entity_id=entity_a.id,
            minimum_amount=Decimal(30000000),
        ))
        db.add(LiquidityThreshold(
            scope_type=LiquidityScopeType.ENTITY, legal_entity_id=entity_b.id,
            minimum_amount=Decimal(200000),
        ))

        db.add(RecurringCashFlow(
            name="[DEMO] Monthly Payroll - Entity A", legal_entity_id=entity_a.id,
            currency_code="NGN", category_code="PAYROLL", amount=Decimal(8000000),
            direction=CashDirection.OUTFLOW, start_date=datetime.date(2026, 9, 25),
            frequency=RecurringFrequency.MONTHLY, notes="DEMO DATA",
        ))
        db.add(RecurringCashFlow(
            name="[DEMO] Monthly Operating Expenses - Entity B", legal_entity_id=entity_b.id,
            currency_code="USD", category_code="OPERATING_EXPENSES", amount=Decimal(15000),
            direction=CashDirection.OUTFLOW, start_date=datetime.date(2026, 9, 20),
            frequency=RecurringFrequency.MONTHLY, notes="DEMO DATA",
        ))

        db.add(ExpectedCollection(
            legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 9, 16),
            currency_code="NGN", amount=Decimal(4000000), counterparty="[DEMO] Retail Customer",
            category="Trade Receivable", probability=85, source_type="DEMO_DATA",
        ))
        db.add(ExpectedCollection(
            legal_entity_id=entity_b.id, expected_date=datetime.date(2026, 9, 18),
            currency_code="USD", amount=Decimal(50000), counterparty="[DEMO] US Partner",
            category="Trade Receivable", probability=90, source_type="DEMO_DATA",
        ))
        db.add(ExpectedPayment(
            legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 9, 30),
            currency_code="NGN", amount=Decimal(55000000), counterparty="[DEMO] Tax Authority",
            category="Taxes", priority="HIGH", probability=100, source_type="DEMO_DATA",
            notes="DEMO DATA - deliberately large to demonstrate a liquidity gap alert",
        ))
        db.add(ExpectedPayment(
            legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 10, 12),
            currency_code="NGN", amount=Decimal(3000000), counterparty="[DEMO] Packaging Supplier",
            category="Trade Payable", probability=95, source_type="DEMO_DATA",
        ))
        db.add(ExpectedCollection(
            legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 11, 2),
            currency_code="NGN", amount=Decimal(70000000), counterparty="[DEMO] Major Distributor",
            category="Trade Receivable", probability=80, source_type="DEMO_DATA",
            notes="DEMO DATA - large recovery collection demonstrating a surplus period",
        ))
        db.add(ExpectedCollection(
            legal_entity_id=entity_b.id, expected_date=datetime.date(2026, 11, 6),
            currency_code="USD", amount=Decimal(80000), counterparty="[DEMO] US Partner",
            category="Trade Receivable", probability=85, source_type="DEMO_DATA",
        ))
        await db.flush()

        forecast = Forecast(
            group_id=group.id, legal_entity_id=None, forecast_start_date=FORECAST_START,
            forecast_end_date=FORECAST_START + datetime.timedelta(days=13 * 7 - 1),
            scenario=ForecastScenarioType.BASE, value_basis=ForecastValueBasis.PROBABILITY_ADJUSTED,
            reporting_currency_code="USD", status=ForecastStatus.DRAFT,
            assumptions_notes="[DEMO] Base case, group-wide, USD-consolidated",
        )
        db.add(forecast)
        await db.flush()
        await calculate_forecast(db, forecast)
        await db.commit()

    print("Stage 3 forecast demo data seeded.")
    print(f"  Demo forecast id: {forecast.id} (DRAFT, group-wide, BASE scenario, USD)")


if __name__ == "__main__":
    asyncio.run(seed())
