"""
Forecast lifecycle: create, roll forward (SECTION 4), publish (SECTION 5/41).
"""
import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.forecast import Forecast, ForecastScenarioAssumption, ForecastStatus


async def roll_forecast(db: AsyncSession, parent: Forecast, created_by_user_id: uuid.UUID | None) -> Forecast:
    """
    Creates the next rolling version: start date advances by one week,
    everything else carries over from the parent (SECTION 4 example:
    W1-W13 -> W2-W14). The parent forecast is left untouched - rolling
    forward never overwrites history.
    """
    new_start = parent.forecast_start_date + datetime.timedelta(days=7)
    new_end = new_start + datetime.timedelta(days=7 * 13 - 1)

    new_forecast = Forecast(
        group_id=parent.group_id, legal_entity_id=parent.legal_entity_id,
        forecast_start_date=new_start, forecast_end_date=new_end,
        scenario=parent.scenario, value_basis=parent.value_basis,
        reporting_currency_code=parent.reporting_currency_code,
        version=parent.version + 1, parent_forecast_id=parent.id,
        status=ForecastStatus.DRAFT, assumptions_notes=parent.assumptions_notes,
        created_by_user_id=created_by_user_id,
    )
    db.add(new_forecast)
    await db.flush()

    parent_assumptions = (
        await db.execute(
            select(ForecastScenarioAssumption).where(
                ForecastScenarioAssumption.forecast_id == parent.id
            )
        )
    ).scalars().all()
    for assumption in parent_assumptions:
        db.add(ForecastScenarioAssumption(
            forecast_id=new_forecast.id, assumption_type=assumption.assumption_type,
            numeric_value=assumption.numeric_value, currency_code=assumption.currency_code,
            category_code=assumption.category_code, description=assumption.description,
        ))
    await db.flush()
    return new_forecast


async def publish_forecast(
    db: AsyncSession, forecast: Forecast, published_by_user_id: uuid.UUID
) -> Forecast:
    if forecast.status == ForecastStatus.PUBLISHED:
        raise ValueError("Forecast is already published.")
    if not forecast.weeks:
        raise ValueError("Forecast must be calculated before it can be published.")
    forecast.status = ForecastStatus.PUBLISHED
    forecast.published_by_user_id = published_by_user_id
    forecast.published_at = datetime.datetime.now(datetime.UTC)
    await db.flush()
    return forecast


async def archive_forecast(db: AsyncSession, forecast: Forecast) -> Forecast:
    forecast.status = ForecastStatus.ARCHIVED
    await db.flush()
    return forecast
