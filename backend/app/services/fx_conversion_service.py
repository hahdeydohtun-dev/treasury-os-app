"""
Currency conversion helper for the forecast engine, built entirely on the
existing FXRate architecture (Stage 1/2) - no second FX system.

Looks up the latest current SPOT rate on or before a given date for
(from_currency, to_currency); if the direct pair isn't found, tries the
inverse and takes the reciprocal. Returns None (never a guessed rate) if
no usable rate exists, so the forecast engine can record a data-quality
warning instead of silently using an incorrect value (SECTION 46).
"""
import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.currency import FXRate, FXRateType


class FXConversionResult:
    def __init__(self, rate: Decimal, rate_type: str, rate_date: datetime.date):
        self.rate = rate
        self.rate_type = rate_type
        self.rate_date = rate_date


async def get_conversion_rate(
    db: AsyncSession,
    from_currency: str,
    to_currency: str,
    as_of: datetime.date,
    rate_type: FXRateType = FXRateType.SPOT,
) -> FXConversionResult | None:
    if from_currency == to_currency:
        return FXConversionResult(Decimal(1), rate_type.value, as_of)

    stmt = (
        select(FXRate)
        .where(
            FXRate.from_currency_code == from_currency,
            FXRate.to_currency_code == to_currency,
            FXRate.rate_type == rate_type,
            FXRate.rate_date <= as_of,
            FXRate.is_current.is_(True),
        )
        .order_by(FXRate.rate_date.desc())
    )
    result = (await db.execute(stmt)).scalars().first()
    if result is not None:
        return FXConversionResult(result.rate, result.rate_type.value, result.rate_date)

    # Try the inverse pair
    inverse_stmt = (
        select(FXRate)
        .where(
            FXRate.from_currency_code == to_currency,
            FXRate.to_currency_code == from_currency,
            FXRate.rate_type == rate_type,
            FXRate.rate_date <= as_of,
            FXRate.is_current.is_(True),
        )
        .order_by(FXRate.rate_date.desc())
    )
    inverse = (await db.execute(inverse_stmt)).scalars().first()
    if inverse is not None and inverse.rate != 0:
        return FXConversionResult(
            Decimal(1) / inverse.rate, inverse.rate_type.value, inverse.rate_date
        )

    return None
