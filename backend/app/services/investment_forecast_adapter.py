"""
Investment -> Forecast adapter (SECTION 20).

The "clean adapter" this codebase's convention requires (same pattern as
app/services/facility_forecast_adapter.py) - it does not modify
app/services/forecast_engine.py's core logic. Gathers, within a
forecast's horizon: scheduled investment maturities (principal +
AT_MATURITY interest, split into two lines so each is separately
traceable), periodic interest receipt dates for PERIODIC investments,
upfront interest for a still-pending UPFRONT investment's future
placement, and planned future placements. Every returned row carries
source_type/source_id so a forecast amount can always be traced back to
the investment (and, for periodic interest, the specific period) that
produced it.

Does NOT invent a maturity cash flow for an investment with no
scheduled/expected settlement - only ACTIVE or PARTIALLY_TERMINATED
investments (an actual outstanding placement) contribute a maturity
line, and only for their currently outstanding principal, never a
stale original amount.

Periodic interest is gathered INDEPENDENTLY of whether the investment's
own maturity date falls inside this specific forecast's horizon - a
monthly-interest investment maturing well beyond a 13-week horizon still
has interest payment dates that fall WITHIN that horizon, and those must
still be forecast (this is exactly what SECTION 6 of the Stage 4
financial-integrity patch requires).
"""
import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment import (
    InterestPaymentMethod,
    Investment,
    InvestmentStatus,
    InvestmentTransaction,
    InvestmentTransactionStatus,
    InvestmentTransactionType,
)
from app.models.lookup import CashDirection
from app.services.investment_engine import generate_periodic_interest_schedule


async def gather_investment_forecast_events(
    db: AsyncSession, entities: list, horizon_start: datetime.date, horizon_end: datetime.date,
) -> list:
    entity_ids = [e.id for e in entities]
    if not entity_ids:
        return []

    events: list = []

    # --- Scheduled maturities (own maturity date within the horizon): principal always; AT_MATURITY interest only ---
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
        # SECTION 8 (Stage 4 financial-integrity hardening): only an
        # AT_MATURITY investment's interest is correctly represented as a
        # single lump sum on the maturity date - PERIODIC and UPFRONT are
        # handled by their own dedicated blocks below, on their own real
        # payment dates, never duplicated here too.
        if (
            investment.interest_payment_method == InterestPaymentMethod.AT_MATURITY
            and investment.expected_interest and investment.expected_interest > 0
        ):
            events.append({
                "legal_entity_id": investment.legal_entity_id, "currency_code": investment.currency_code,
                "category_code": "INVESTMENT_MATURITIES", "direction": CashDirection.INFLOW,
                "event_date": investment.maturity_date, "amount": investment.expected_interest,
                "source_type": "INVESTMENT_MATURITY_INTEREST", "source_id": str(investment.id),
                "description": f"{investment.investment_reference}: Maturity interest",
                "counterparty": investment.investment_reference,
            })

    # --- PERIODIC interest: gathered independently of whether the
    # investment's OWN maturity falls inside this horizon - a payment
    # date can fall inside the horizon even when maturity is much later. ---
    periodic_stmt = select(Investment).where(
        Investment.legal_entity_id.in_(entity_ids),
        Investment.status.in_((InvestmentStatus.ACTIVE, InvestmentStatus.PARTIALLY_TERMINATED)),
        Investment.interest_payment_method == InterestPaymentMethod.PERIODIC,
        Investment.interest_payment_frequency.is_not(None),
        Investment.is_active.is_(True),
    )
    for investment in (await db.execute(periodic_stmt)).scalars().all():
        schedule = generate_periodic_interest_schedule(
            investment.principal_amount, investment.interest_rate, investment.start_date,
            investment.maturity_date, investment.interest_payment_frequency,
            investment.day_count_convention,
        )

        # SECTION 3 (final freeze patch): do not re-forecast a period
        # whose interest has already been recorded as actually received.
        # The current data model has no explicit "this InvestmentTransaction
        # settles period N" link, so the best available, non-invented
        # signal is an EXACT date match: a manually or Excel-recorded
        # INTEREST_RECEIPT transaction dated exactly on a period's own
        # end date is treated as that period having been realized.
        # Documented limitation (see docs/STAGE_4_INVESTMENTS.md, "Known
        # limitations"): a receipt recorded a day early/late (rather than
        # exactly on the theoretical period_end) will not be matched and
        # that period would still be forecast - a genuine, stated
        # boundary rather than invented reconciliation logic.
        received_stmt = select(InvestmentTransaction.transaction_date).where(
            InvestmentTransaction.investment_id == investment.id,
            InvestmentTransaction.transaction_type == InvestmentTransactionType.INTEREST_RECEIPT,
            InvestmentTransaction.status == InvestmentTransactionStatus.EXECUTED,
        )
        already_received_dates = {row[0] for row in (await db.execute(received_stmt)).all()}

        for entry in schedule:
            if entry.interest_amount <= 0:
                continue
            if not (horizon_start <= entry.period_end <= horizon_end):
                continue
            if entry.period_end in already_received_dates:
                continue
            events.append({
                "legal_entity_id": investment.legal_entity_id, "currency_code": investment.currency_code,
                "category_code": "INVESTMENT_MATURITIES", "direction": CashDirection.INFLOW,
                "event_date": entry.period_end, "amount": entry.interest_amount,
                "source_type": "INVESTMENT_INTEREST_RECEIPT",
                "source_id": f"{investment.id}:period-{entry.period}",
                "description": f"{investment.investment_reference}: Period {entry.period} "
                                f"interest ({entry.period_start} to {entry.period_end})",
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
        # SECTION 6: UPFRONT interest is received AT the placement date -
        # a single lump sum then, never at maturity. Only forecast it here
        # while the investment hasn't been placed yet (once ACTIVE, the
        # upfront interest has already happened and is a historical
        # InvestmentTransaction, not a forecast line).
        if (
            investment.interest_payment_method == InterestPaymentMethod.UPFRONT
            and investment.expected_interest and investment.expected_interest > 0
        ):
            events.append({
                "legal_entity_id": investment.legal_entity_id, "currency_code": investment.currency_code,
                "category_code": "INVESTMENT_MATURITIES", "direction": CashDirection.INFLOW,
                "event_date": investment.placement_date, "amount": investment.expected_interest,
                "source_type": "INVESTMENT_INTEREST_RECEIPT", "source_id": str(investment.id),
                "description": f"{investment.investment_reference}: Upfront interest",
                "counterparty": investment.investment_reference,
            })

    return events
