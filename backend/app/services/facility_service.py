"""
Facility orchestration service: lifecycle transitions, versioning on
update, and drawdown/repayment state changes. Pure calculation lives in
facility_engine.py; this module handles persistence + audit trail.

Concurrency: every function here that mutates a shared financial balance
(current_drawn_amount, a FacilitySubLimit's drawn_amount, a repayment's
paid_amount) is called by its API endpoint only AFTER that row (and the
related Facility row) has been loaded with SELECT ... FOR UPDATE
(see the load_*_for_update helpers below and app/api/v1/facilities.py) -
this module does not re-issue its own locking query, so it must never be
called on a row obtained through an unlocked db.get(...). See
docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md, "Concurrency and idempotency",
for the full design and rationale.
"""
import datetime
import uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.facility import (
    FACILITY_STATUS_TRANSITIONS,
    Facility,
    FacilityDrawdown,
    FacilityEvent,
    FacilityEventType,
    FacilityRepayment,
    FacilityStatus,
    FacilitySubLimit,
    FacilityVersion,
    RepaymentStatus,
)


async def load_facility_for_update(db: AsyncSession, facility_id: uuid.UUID) -> Facility | None:
    """
    Loads a Facility with a Postgres row lock (SELECT ... FOR UPDATE),
    held until the enclosing request's transaction commits or rolls back.
    Any endpoint that reads current_drawn_amount in order to decide
    whether a new drawdown/repayment is safe MUST load the facility this
    way, not via db.get(...) - otherwise two concurrent requests can both
    read the same pre-mutation balance and both proceed, producing the
    double-drawdown overdraw scenario this function exists to prevent
    (SECTION 3).
    """
    result = await db.execute(select(Facility).where(Facility.id == facility_id).with_for_update())
    return result.scalar_one_or_none()


async def load_sub_limit_for_update(db: AsyncSession, sub_limit_id: uuid.UUID) -> FacilitySubLimit | None:
    result = await db.execute(
        select(FacilitySubLimit).where(FacilitySubLimit.id == sub_limit_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def load_drawdown_for_update(db: AsyncSession, drawdown_id: uuid.UUID) -> FacilityDrawdown | None:
    result = await db.execute(
        select(FacilityDrawdown).where(FacilityDrawdown.id == drawdown_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def load_repayment_for_update(db: AsyncSession, repayment_id: uuid.UUID) -> FacilityRepayment | None:
    result = await db.execute(
        select(FacilityRepayment).where(FacilityRepayment.id == repayment_id).with_for_update()
    )
    return result.scalar_one_or_none()


def _facility_terms_snapshot(facility: Facility) -> dict:
    return {
        "committed_limit": str(facility.committed_limit),
        "approved_limit": str(facility.approved_limit),
        "fixed_rate": str(facility.fixed_rate) if facility.fixed_rate else None,
        "benchmark_rate": str(facility.benchmark_rate) if facility.benchmark_rate else None,
        "spread": str(facility.spread) if facility.spread else None,
        "maturity_date": str(facility.maturity_date),
        "repayment_method": facility.repayment_method.value,
        "repayment_frequency": facility.repayment_frequency,
    }


async def record_initial_version(db: AsyncSession, facility: Facility, user_id) -> None:
    db.add(FacilityVersion(
        facility_id=facility.id, version=1, effective_date=facility.start_date,
        terms=_facility_terms_snapshot(facility), change_reason="Initial facility creation",
        created_by_user_id=user_id,
    ))
    db.add(FacilityEvent(
        facility_id=facility.id, event_type=FacilityEventType.FACILITY_CREATED,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"Facility {facility.facility_reference} created.",
        new_value=_facility_terms_snapshot(facility), created_by_user_id=user_id,
    ))


async def apply_facility_update(
    db: AsyncSession, facility: Facility, changes: dict, change_reason: str, user_id,
) -> Facility:
    previous_terms = _facility_terms_snapshot(facility)

    for field, value in changes.items():
        if value is not None:
            setattr(facility, field, value)
    facility.version += 1
    facility.updated_by_user_id = user_id

    new_terms = _facility_terms_snapshot(facility)
    db.add(FacilityVersion(
        facility_id=facility.id, version=facility.version, effective_date=datetime.date.today(),
        terms=new_terms, change_reason=change_reason, created_by_user_id=user_id,
    ))

    event_type = (
        FacilityEventType.LIMIT_CHANGED if "committed_limit" in changes else FacilityEventType.RATE_CHANGED
    )
    if "maturity_date" in changes:
        event_type = FacilityEventType.MATURITY_EXTENDED
    db.add(FacilityEvent(
        facility_id=facility.id, event_type=event_type,
        event_date=datetime.datetime.now(datetime.UTC),
        description=change_reason, previous_value=previous_terms, new_value=new_terms,
        created_by_user_id=user_id,
    ))
    await db.flush()
    return facility


async def transition_facility_status(
    db: AsyncSession, facility: Facility, new_status: FacilityStatus, reason, user_id,
) -> Facility:
    allowed = FACILITY_STATUS_TRANSITIONS.get(facility.status, set())
    if new_status not in allowed and new_status != facility.status:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition facility from {facility.status.value} to "
                   f"{new_status.value}. Allowed: {[s.value for s in allowed] or 'none'}.",
        )
    previous_status = facility.status
    facility.status = new_status

    event_type = FacilityEventType.STATUS_CHANGED
    if new_status == FacilityStatus.ACTIVE and previous_status == FacilityStatus.DRAFT:
        event_type = FacilityEventType.FACILITY_ACTIVATED
    elif new_status == FacilityStatus.SUSPENDED:
        event_type = FacilityEventType.FACILITY_SUSPENDED
    elif new_status == FacilityStatus.CLOSED:
        event_type = FacilityEventType.FACILITY_CLOSED
    elif new_status == FacilityStatus.RENEWED:
        event_type = FacilityEventType.RENEWED

    db.add(FacilityEvent(
        facility_id=facility.id, event_type=event_type,
        event_date=datetime.datetime.now(datetime.UTC),
        description=reason or f"Status changed from {previous_status.value} to {new_status.value}.",
        previous_value={"status": previous_status.value}, new_value={"status": new_status.value},
        created_by_user_id=user_id,
    ))
    await db.flush()
    return facility


async def execute_drawdown(
    db: AsyncSession, facility: Facility, drawdown: FacilityDrawdown, user_id,
) -> None:
    from app.models.facility import DrawdownStatus, FacilitySubLimit

    drawdown.status = DrawdownStatus.EXECUTED
    drawdown.executed_by_user_id = user_id
    facility.current_drawn_amount += drawdown.drawdown_amount

    if drawdown.sub_limit_id is not None:
        sub_limit = await db.get(FacilitySubLimit, drawdown.sub_limit_id)
        if sub_limit is not None:
            sub_limit.drawn_amount += drawdown.drawdown_amount

    db.add(FacilityEvent(
        facility_id=facility.id, event_type=FacilityEventType.DRAW_DOWN,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"Drawdown of {drawdown.drawdown_amount} {drawdown.currency_code} executed.",
        related_record_type="FacilityDrawdown", related_record_id=str(drawdown.id),
        new_value={"drawdown_amount": str(drawdown.drawdown_amount)}, created_by_user_id=user_id,
    ))
    await db.flush()


async def record_repayment_payment(
    db: AsyncSession, facility: Facility, repayment: FacilityRepayment, paid_amount: Decimal,
    payment_date: datetime.date, user_id,
) -> None:
    from app.models.facility import FacilitySubLimit, RepaymentType

    # SECTION 4: a repayment cannot exceed the outstanding principal
    # unless a business rule explicitly permits overpayment - no such
    # rule exists yet, so this is rejected outright rather than silently
    # allowed (which would also let current_drawn_amount go negative
    # for a PRINCIPAL repayment).
    remaining = repayment.original_amount - repayment.paid_amount
    if paid_amount > remaining:
        raise HTTPException(
            status_code=400,
            detail=f"Payment of {paid_amount} exceeds the outstanding balance of {remaining} "
                   "on this repayment. Overpayment is not currently supported.",
        )

    repayment.paid_amount += paid_amount
    repayment.actual_payment_date = payment_date
    if repayment.paid_amount >= repayment.original_amount:
        repayment.status = RepaymentStatus.PAID
    else:
        repayment.status = RepaymentStatus.PARTIALLY_PAID

    if repayment.repayment_type == RepaymentType.PRINCIPAL:
        facility.current_drawn_amount = max(Decimal(0), facility.current_drawn_amount - paid_amount)

        # If this repayment is tied to a specific drawdown that was itself
        # drawn against a sub-limit, free up that sub-limit's capacity too -
        # otherwise a sub-limit would only ever fill up and never recover,
        # permanently understating what's actually available under it.
        if repayment.drawdown_id is not None:
            drawdown = await db.get(FacilityDrawdown, repayment.drawdown_id)
            if drawdown is not None and drawdown.sub_limit_id is not None:
                sub_limit = await db.get(FacilitySubLimit, drawdown.sub_limit_id)
                if sub_limit is not None:
                    sub_limit.drawn_amount = max(Decimal(0), sub_limit.drawn_amount - paid_amount)

    event_type = (
        FacilityEventType.EARLY_REPAYMENT if repayment.is_early_repayment
        else FacilityEventType.REPAYMENT
    )
    db.add(FacilityEvent(
        facility_id=facility.id, event_type=event_type,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"{repayment.repayment_type.value.title()} payment of {paid_amount} "
                    f"{repayment.currency_code} recorded.",
        related_record_type="FacilityRepayment", related_record_id=str(repayment.id),
        new_value={"paid_amount": str(paid_amount), "status": repayment.status.value},
        created_by_user_id=user_id,
    ))
    await db.flush()
