"""
Facility -> Forecast adapter (SECTION 23).

This is the "clean funding adapter/service" the spec requires instead of
rewriting the forecast engine: it gathers facility-driven cash-flow
events (scheduled repayments, fees, approved future drawdowns) in a
shape app/services/forecast_engine.py can convert into its existing
_RawLine rows, so the engine's gathering/aggregation logic picks them up
with no special-casing. Every returned row carries source_type/source_id
(SECTION 23 example: source_type=FACILITY_REPAYMENT, source_id=repayment_id)
so a forecast amount can always be traced back to the facility record
that produced it.
"""
import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.facility import (
    DrawdownStatus,
    Facility,
    FacilityDrawdown,
    FacilityFee,
    FacilityRepayment,
    FeeStatus,
    RepaymentStatus,
    RepaymentType,
)
from app.models.lookup import CashDirection

_REPAYMENT_TYPE_TO_CATEGORY = {
    RepaymentType.PRINCIPAL: "DEBT_PRINCIPAL",
    RepaymentType.INTEREST: "INTEREST",
    RepaymentType.FEE: "BANK_CHARGES",
}

_OPEN_REPAYMENT_STATUSES = (
    RepaymentStatus.SCHEDULED, RepaymentStatus.DUE, RepaymentStatus.PARTIALLY_PAID,
    RepaymentStatus.OVERDUE,
)


async def gather_facility_forecast_events(
    db: AsyncSession, entities: list, horizon_start: datetime.date, horizon_end: datetime.date,
) -> list:
    entity_ids = [e.id for e in entities]
    if not entity_ids:
        return []

    events: list = []

    repayment_stmt = (
        select(FacilityRepayment, Facility)
        .join(Facility, Facility.id == FacilityRepayment.facility_id)
        .where(
            FacilityRepayment.legal_entity_id.in_(entity_ids),
            FacilityRepayment.due_date >= horizon_start,
            FacilityRepayment.due_date <= horizon_end,
            FacilityRepayment.status.in_(_OPEN_REPAYMENT_STATUSES),
        )
    )
    for repayment, facility in (await db.execute(repayment_stmt)).all():
        remaining = repayment.original_amount - repayment.paid_amount
        if remaining <= 0:
            continue
        category = _REPAYMENT_TYPE_TO_CATEGORY.get(repayment.repayment_type, "OTHER_OUTFLOW")
        source_type = {
            RepaymentType.PRINCIPAL: "FACILITY_REPAYMENT",
            RepaymentType.INTEREST: "FACILITY_INTEREST",
            RepaymentType.FEE: "FACILITY_FEE",
        }[repayment.repayment_type]
        events.append({
            "legal_entity_id": repayment.legal_entity_id, "currency_code": repayment.currency_code,
            "category_code": category, "direction": CashDirection.OUTFLOW,
            "event_date": repayment.due_date, "amount": remaining,
            "source_type": source_type, "source_id": str(repayment.id),
            "description": f"{facility.facility_name}: {repayment.repayment_type.value.title()} due",
            "counterparty": facility.facility_name,
        })

    fee_stmt = (
        select(FacilityFee, Facility)
        .join(Facility, Facility.id == FacilityFee.facility_id)
        .where(
            FacilityFee.legal_entity_id.in_(entity_ids),
            FacilityFee.due_date >= horizon_start,
            FacilityFee.due_date <= horizon_end,
            FacilityFee.status == FeeStatus.DUE,
        )
    )
    for fee, facility in (await db.execute(fee_stmt)).all():
        events.append({
            "legal_entity_id": fee.legal_entity_id, "currency_code": fee.currency_code,
            "category_code": "BANK_CHARGES", "direction": CashDirection.OUTFLOW,
            "event_date": fee.due_date, "amount": fee.amount,
            "source_type": "FACILITY_FEE", "source_id": str(fee.id),
            "description": f"{facility.facility_name}: {fee.fee_type.value.title()} fee",
            "counterparty": facility.facility_name,
        })

    drawdown_stmt = (
        select(FacilityDrawdown, Facility)
        .join(Facility, Facility.id == FacilityDrawdown.facility_id)
        .where(
            FacilityDrawdown.legal_entity_id.in_(entity_ids),
            FacilityDrawdown.drawdown_date >= horizon_start,
            FacilityDrawdown.drawdown_date <= horizon_end,
            FacilityDrawdown.status == DrawdownStatus.APPROVED,
        )
    )
    for drawdown, facility in (await db.execute(drawdown_stmt)).all():
        events.append({
            "legal_entity_id": drawdown.legal_entity_id, "currency_code": drawdown.currency_code,
            "category_code": "LOAN_DRAWDOWNS", "direction": CashDirection.INFLOW,
            "event_date": drawdown.drawdown_date, "amount": drawdown.drawdown_amount,
            "source_type": "FACILITY_DRAWDOWN", "source_id": str(drawdown.id),
            "description": f"{facility.facility_name}: Approved drawdown",
            "counterparty": facility.facility_name,
        })

    return events
