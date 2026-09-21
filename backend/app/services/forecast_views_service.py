"""
Entity-level and currency-level forecast views (SECTION 6/37/38).

Rather than storing a redundant ForecastWeek row per entity/currency
combination, these views aggregate the already-calculated ForecastLine
rows on demand:

- Entity view sums each line's reporting_amount (already FX-converted
  at calculation time), filtered by legal_entity_id - consistent with
  the consolidated group figures.
- Currency view sums each line's adjusted_amount (the ORIGINAL,
  UN-converted transaction-currency amount), filtered by
  transaction_currency_code, with its own running native-currency
  opening/closing balance - so a currency-specific shortfall is never
  hidden by FX conversion or group consolidation (SECTION 7/38).
"""
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.forecast import Forecast, ForecastLine
from app.models.lookup import CashDirection


async def get_entity_view(db: AsyncSession, forecast: Forecast, legal_entity_id: uuid.UUID) -> list:
    weeks = sorted(forecast.weeks, key=lambda w: w.week_number)
    lines_result = await db.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.legal_entity_id == legal_entity_id,
        )
    )
    lines = list(lines_result.scalars().all())

    rows = []
    for week in weeks:
        week_lines = [ln for ln in lines if ln.week_id == week.id]
        inflows = sum(
            (ln.reporting_amount for ln in week_lines if ln.direction == CashDirection.INFLOW),
            Decimal(0),
        )
        outflows = sum(
            (ln.reporting_amount for ln in week_lines if ln.direction == CashDirection.OUTFLOW),
            Decimal(0),
        )
        transfers = sum(
            (ln.reporting_amount for ln in week_lines if ln.direction == CashDirection.TRANSFER),
            Decimal(0),
        )
        rows.append({
            "week_number": week.week_number, "start_date": week.start_date,
            "end_date": week.end_date, "total_inflows": inflows, "total_outflows": outflows,
            "net_transfers": transfers, "net_cash_flow": inflows - outflows + transfers,
        })
    return rows


async def get_currency_view(db: AsyncSession, forecast: Forecast, currency_code: str) -> list:
    weeks = sorted(forecast.weeks, key=lambda w: w.week_number)
    lines_result = await db.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.transaction_currency_code == currency_code,
        )
    )
    lines = list(lines_result.scalars().all())

    running = Decimal(0)
    for key, amount in forecast.opening_cash_snapshot.items():
        if key.endswith(f"|{currency_code}"):
            running += Decimal(amount)

    rows = []
    for week in weeks:
        week_lines = [ln for ln in lines if ln.week_id == week.id]
        inflows = sum(
            (ln.adjusted_amount for ln in week_lines if ln.direction == CashDirection.INFLOW),
            Decimal(0),
        )
        outflows = sum(
            (ln.adjusted_amount for ln in week_lines if ln.direction == CashDirection.OUTFLOW),
            Decimal(0),
        )
        transfers = sum(
            (ln.adjusted_amount for ln in week_lines if ln.direction == CashDirection.TRANSFER),
            Decimal(0),
        )
        net = inflows - outflows + transfers
        opening = running
        closing = opening + net
        running = closing
        rows.append({
            "week_number": week.week_number, "start_date": week.start_date,
            "end_date": week.end_date, "opening_cash": opening, "total_inflows": inflows,
            "total_outflows": outflows, "net_transfers": transfers, "net_cash_flow": net,
            "closing_cash": closing,
        })
    return rows
