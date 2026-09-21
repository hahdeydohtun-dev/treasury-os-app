"""
Forecast-vs-actual variance and accuracy (SECTION 25/26).

Deliberately a read-model, not a persisted table: recomputed from
ForecastLine (the forecast) and TreasuryTransaction (the actuals) each
time, so it can never drift out of sync with its sources.

Variance sign convention (SECTION 25 - "must clearly distinguish"):
  variance_amount = actual_amount - forecast_amount
A positive variance means actual cash flow exceeded the forecast for an
INFLOW category (better than expected) or actual outflow exceeded the
forecast for an OUTFLOW category (worse than expected) - the API always
labels which direction the number is for, so this is never ambiguous.
"""
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.forecast import Forecast, ForecastLine, ForecastSourceType, WeekStatus
from app.models.lookup import CashDirection


@dataclass
class VarianceRow:
    week_number: int
    legal_entity_id: uuid.UUID | None
    currency_code: str | None
    category_code: str | None
    forecast_amount: Decimal
    actual_amount: Decimal
    variance_amount: Decimal
    variance_percentage: Decimal | None


def _variance_pct(forecast_amount: Decimal, variance_amount: Decimal) -> Decimal | None:
    if forecast_amount == 0:
        return None
    return (variance_amount / abs(forecast_amount) * 100).quantize(Decimal("0.01"))


async def compute_variance(
    db: AsyncSession, forecast: Forecast, group_by: str = "week"
) -> list[VarianceRow]:
    """
    group_by: "week" | "entity" | "currency" | "category" | "group"
    Only COMPLETED weeks are compared (a future week has no actuals yet).
    """
    completed_weeks = {w.id: w.week_number for w in forecast.weeks if w.status == WeekStatus.COMPLETED}
    if not completed_weeks:
        return []

    lines_result = await db.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.week_id.in_(completed_weeks.keys()),
        )
    )
    lines = list(lines_result.scalars().all())

    def key_for(line: ForecastLine):
        if group_by == "week":
            return completed_weeks[line.week_id]
        if group_by == "entity":
            return line.legal_entity_id
        if group_by == "currency":
            return line.transaction_currency_code
        if group_by == "category":
            return line.category_code
        return "GROUP"

    buckets: dict = {}
    for line in lines:
        k = key_for(line)
        bucket = buckets.setdefault(k, {"forecast": Decimal(0), "actual": Decimal(0)})
        signed = line.reporting_amount if line.direction != CashDirection.OUTFLOW else -line.reporting_amount
        if line.source_type == ForecastSourceType.ACTUAL:
            bucket["actual"] += signed
        else:
            bucket["forecast"] += signed

    rows = []
    for k, bucket in buckets.items():
        variance = bucket["actual"] - bucket["forecast"]
        rows.append(VarianceRow(
            week_number=k if group_by == "week" else None,
            legal_entity_id=k if group_by == "entity" else None,
            currency_code=k if group_by == "currency" else None,
            category_code=k if group_by == "category" else None,
            forecast_amount=bucket["forecast"], actual_amount=bucket["actual"],
            variance_amount=variance, variance_percentage=_variance_pct(bucket["forecast"], variance),
        ))
    return rows


async def compute_accuracy(db: AsyncSession, forecast: Forecast, target_variance_pct: Decimal) -> dict:
    """
    Overall + inflow/outflow accuracy = 1 - (|variance| / |forecast|), clamped to
    [0, 1], averaged across completed weeks with a forecast amount. Also
    reports the configured KPI target (default: SECTION 26 says <=5%,
    exposed as a parameter, never hard-coded as a system failure).
    """
    overall = await compute_variance(db, forecast, group_by="week")
    if not overall:
        return {
            "overall_accuracy": None, "inflow_accuracy": None, "outflow_accuracy": None,
            "target_variance_pct": target_variance_pct, "actual_variance_pct": None,
            "within_target": None, "weeks_measured": 0,
        }

    total_forecast = sum((abs(r.forecast_amount) for r in overall), Decimal(0))
    total_variance = sum((abs(r.variance_amount) for r in overall), Decimal(0))
    actual_variance_pct = (
        (total_variance / total_forecast * 100) if total_forecast > 0 else None
    )

    by_category = await compute_variance(db, forecast, group_by="category")
    inflow_rows = [r for r in by_category if r.forecast_amount > 0]
    outflow_rows = [r for r in by_category if r.forecast_amount < 0]

    def _accuracy(rows) -> Decimal | None:
        if not rows:
            return None
        f = sum((abs(r.forecast_amount) for r in rows), Decimal(0))
        v = sum((abs(r.variance_amount) for r in rows), Decimal(0))
        if f == 0:
            return None
        return max(Decimal(0), Decimal(1) - (v / f))

    overall_accuracy = (
        max(Decimal(0), Decimal(1) - (total_variance / total_forecast))
        if total_forecast > 0 else None
    )

    return {
        "overall_accuracy": overall_accuracy,
        "inflow_accuracy": _accuracy(inflow_rows),
        "outflow_accuracy": _accuracy(outflow_rows),
        "target_variance_pct": target_variance_pct,
        "actual_variance_pct": (
            actual_variance_pct.quantize(Decimal("0.01")) if actual_variance_pct is not None else None
        ),
        "within_target": (
            actual_variance_pct <= target_variance_pct if actual_variance_pct is not None else None
        ),
        "weeks_measured": len(overall),
    }
