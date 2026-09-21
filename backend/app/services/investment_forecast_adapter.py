"""
Investment -> Forecast adapter (SECTION 20).

The "clean adapter" this codebase's convention requires (same pattern as
app/services/facility_forecast_adapter.py) - it does not modify
app/services/forecast_engine.py's core logic. Gathers, within a
forecast's horizon: scheduled investment maturities (principal +
interest, split into two lines so each is separately traceable) and
executed interest-receipt transactions for periodic-interest
investments. Every returned row carries source_type/source_id so a
forecast amount can always be traced back to the investment or
investment-transaction that produced it.

Does NOT invent a maturity cash flow for an investment with no
scheduled/expected settlement - only ACTIVE or PARTIALLY_TERMINATED
investments (an actual outstanding placement) contribute a maturity
line, and only for their currently outstanding principal, never a
stale original amount.
"""
import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment import Investment, InvestmentStatus
from app.models.lookup import CashDirection


async def gather_investment_forecast_events(
    db: AsyncSession, entities: list, horizon_start: datetime.date, horizon_end: datetime.date,
) -> list:
    entity_ids = [e.id for e in entities]
    if not entity_ids:
        return []

    events: list = []

    # --- Scheduled maturities: principal + interest, each separately traceable ---
    maturity_stmt = select(Investment).where(
        Investment.legal_entity_id.in_(entity_ids),
        Investment.maturity_date >= horizon_start, Investment.maturity_date <= horizon_end,
        Investment.status.in_((InvestmentStatus.ACTIVE, InvestmentStatus.PARTIALLY_TERMINATED)),
        Investment.is_active.is_(True),
    )
    for investment in (await db.execute(maturity_stmt)).scalars().all():
        if investment.principal_amount > 0:
            events.append({
                "legal_entity_id": investment.legal_entity_id, "currency_code": investment.currency_code,
                "category_code": "INVESTMENT_MATURITIES", "direction": CashDirection.INFLOW,
                "event_date": investment.maturity_date, "amount": investment.principal_amount,
                "source_type": "INVESTMENT_MATURITY_PRINCIPAL", "source_id": str(investment.id),
                "description": f"{investment.investment_reference}: Maturity principal",
                "counterparty": investment.investment_reference,
            })
        if investment.expected_interest and investment.expected_interest > 0:
            events.append({
                "legal_entity_id": investment.legal_entity_id, "currency_code": investment.currency_code,
                "category_code": "INVESTMENT_MATURITIES", "direction": CashDirection.INFLOW,
                "event_date": investment.maturity_date, "amount": investment.expected_interest,
                "source_type": "INVESTMENT_MATURITY_INTEREST", "source_id": str(investment.id),
                "description": f"{investment.investment_reference}: Maturity interest",
                "counterparty": investment.investment_reference,
            })

    # --- Planned future placements not yet executed (PLACEMENT_PENDING) ---
    pending_stmt = select(Investment).where(
        Investment.legal_entity_id.in_(entity_ids),
        Investment.status == InvestmentStatus.PLACEMENT_PENDING,
        Investment.placement_date.is_not(None),
        Investment.placement_date >= horizon_start, Investment.placement_date <= horizon_end,
        Investment.is_active.is_(True),
    )
    for investment in (await db.execute(pending_stmt)).scalars().all():
        events.append({
            "legal_entity_id": investment.legal_entity_id, "currency_code": investment.currency_code,
            "category_code": "INVESTMENT_PLACEMENTS", "direction": CashDirection.OUTFLOW,
            "event_date": investment.placement_date, "amount": investment.principal_amount,
            "source_type": "INVESTMENT_PLACEMENT", "source_id": str(investment.id),
            "description": f"{investment.investment_reference}: Planned placement",
            "counterparty": investment.investment_reference,
        })

    return events
