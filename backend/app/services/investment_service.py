"""
Investment orchestration service: lifecycle transitions, versioning on
update, placement, termination (full/partial), rollover, and rebooking.
Pure calculation lives in investment_engine.py; this module handles
persistence + audit trail, mirroring app/services/facility_service.py.

Concurrency (SECTION 39, same discipline as Stage 3): every function
here that mutates a shared financial balance (Investment.principal_amount,
status) is called by its API endpoint only AFTER the row has been loaded
with SELECT ... FOR UPDATE (load_investment_for_update) - this module
does not re-issue its own locking query.
"""
import datetime
import uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment import (
    INVESTMENT_STATUS_TRANSITIONS,
    Investment,
    InvestmentEvent,
    InvestmentEventType,
    InvestmentStatus,
    InvestmentTransaction,
    InvestmentTransactionStatus,
    InvestmentTransactionType,
    InvestmentVersion,
)
from app.services.investment_engine import calculate_early_termination


async def load_investment_for_update(db: AsyncSession, investment_id: uuid.UUID) -> Investment | None:
    result = await db.execute(select(Investment).where(Investment.id == investment_id).with_for_update())
    return result.scalar_one_or_none()


def _investment_terms_snapshot(investment: Investment) -> dict:
    return {
        "principal_amount": str(investment.principal_amount),
        "original_principal_amount": str(investment.original_principal_amount),
        "interest_rate": str(investment.interest_rate),
        "maturity_date": str(investment.maturity_date),
        "tenor_days": investment.tenor_days,
        "interest_payment_method": investment.interest_payment_method.value,
        "early_termination_allowed": investment.early_termination_allowed,
        "partial_termination_allowed": investment.partial_termination_allowed,
        "rollover_allowed": investment.rollover_allowed,
    }


async def record_initial_version(db: AsyncSession, investment: Investment, user_id) -> None:
    db.add(InvestmentVersion(
        investment_id=investment.id, version=1, effective_date=investment.start_date,
        terms=_investment_terms_snapshot(investment), change_reason="Initial investment creation",
        created_by_user_id=user_id,
    ))
    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=InvestmentEventType.INVESTMENT_CREATED,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"Investment {investment.investment_reference} created.",
        new_value=_investment_terms_snapshot(investment), created_by_user_id=user_id,
    ))


async def apply_investment_update(
    db: AsyncSession, investment: Investment, changes: dict, change_reason: str, user_id,
) -> Investment:
    previous_terms = _investment_terms_snapshot(investment)
    for field, value in changes.items():
        if value is not None:
            setattr(investment, field, value)
    investment.version += 1
    investment.updated_by_user_id = user_id

    new_terms = _investment_terms_snapshot(investment)
    db.add(InvestmentVersion(
        investment_id=investment.id, version=investment.version, effective_date=datetime.date.today(),
        terms=new_terms, change_reason=change_reason, created_by_user_id=user_id,
    ))
    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=InvestmentEventType.TERMS_CHANGED,
        event_date=datetime.datetime.now(datetime.UTC), description=change_reason,
        previous_value=previous_terms, new_value=new_terms, created_by_user_id=user_id,
    ))
    await db.flush()
    return investment


async def transition_investment_status(
    db: AsyncSession, investment: Investment, new_status: InvestmentStatus, reason, user_id,
) -> Investment:
    allowed = INVESTMENT_STATUS_TRANSITIONS.get(investment.status, set())
    if new_status not in allowed and new_status != investment.status:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition investment from {investment.status.value} to "
                   f"{new_status.value}. Allowed: {[s.value for s in allowed] or 'none'}.",
        )
    previous_status = investment.status
    investment.status = new_status

    event_type = InvestmentEventType.STATUS_CHANGED
    if new_status == InvestmentStatus.REJECTED:
        event_type = InvestmentEventType.REJECTED
    elif new_status == InvestmentStatus.CANCELLED:
        event_type = InvestmentEventType.CANCELLED
    elif new_status == InvestmentStatus.APPROVED:
        event_type = InvestmentEventType.APPROVED
    elif new_status == InvestmentStatus.SUBMITTED:
        event_type = InvestmentEventType.SUBMITTED

    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=event_type,
        event_date=datetime.datetime.now(datetime.UTC),
        description=reason or f"Status changed from {previous_status.value} to {new_status.value}.",
        previous_value={"status": previous_status.value}, new_value={"status": new_status.value},
        reason=reason, created_by_user_id=user_id,
    ))
    await db.flush()
    return investment


async def place_investment(db: AsyncSession, investment: Investment, user_id) -> InvestmentTransaction:
    """
    SECTION 11/12: idempotent - if a PLACEMENT transaction already
    exists for this investment (checked under the row lock the caller
    already holds), this raises rather than creating a second cash
    movement. The caller (API endpoint) is expected to have already
    checked investment.status; this function focuses on the
    transaction-level idempotency guarantee.
    """
    existing_stmt = select(InvestmentTransaction).where(
        InvestmentTransaction.investment_id == investment.id,
        InvestmentTransaction.transaction_type == InvestmentTransactionType.PLACEMENT,
    )
    existing = (await db.execute(existing_stmt)).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="This investment has already been placed.")

    transaction = InvestmentTransaction(
        investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
        transaction_type=InvestmentTransactionType.PLACEMENT, currency_code=investment.currency_code,
        amount=investment.principal_amount, transaction_date=investment.placement_date or datetime.date.today(),
        status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
        executed_by_user_id=user_id,
    )
    db.add(transaction)
    investment.status = InvestmentStatus.ACTIVE
    investment.placed_by_user_id = user_id

    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=InvestmentEventType.PLACED,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"Placed {investment.principal_amount} {investment.currency_code}.",
        amount=investment.principal_amount, currency_code=investment.currency_code,
        related_record_type="InvestmentTransaction", created_by_user_id=user_id,
    ))
    await db.flush()
    db.add(InvestmentEvent(  # link the transaction id now that it has one, in a second small event field
        investment_id=investment.id, event_type=InvestmentEventType.STATUS_CHANGED,
        event_date=datetime.datetime.now(datetime.UTC), description="Investment is now ACTIVE.",
        previous_value={"status": InvestmentStatus.PLACEMENT_PENDING.value},
        new_value={"status": InvestmentStatus.ACTIVE.value}, created_by_user_id=user_id,
    ))
    await db.flush()
    return transaction


async def terminate_investment(
    db: AsyncSession, investment: Investment, amount: Decimal, termination_date: datetime.date,
    user_id,
) -> tuple:
    """
    SECTION 14/15: validates via the caller (validate_termination_amount)
    before this is invoked. Reduces principal_amount, creates the
    termination transaction (+ a separate PENALTY transaction if a
    penalty applies), records the event, and sets status to
    PARTIALLY_TERMINATED or TERMINATED depending on whether principal
    remains outstanding afterward.
    """
    is_full = amount >= investment.principal_amount
    proportion = amount / investment.original_principal_amount if investment.original_principal_amount else Decimal(1)

    result = calculate_early_termination(
        principal_to_terminate=amount, annual_rate_pct=investment.interest_rate,
        start_date=investment.start_date, termination_date=termination_date,
        convention=investment.day_count_convention, penalty_type=investment.penalty_type,
        penalty_rate=investment.penalty_rate, penalty_amount=investment.penalty_amount,
        proportion_of_total=proportion,
    )

    transaction_type = (
        InvestmentTransactionType.FULL_TERMINATION if is_full
        else InvestmentTransactionType.PARTIAL_TERMINATION
    )
    transaction = InvestmentTransaction(
        investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
        transaction_type=transaction_type, currency_code=investment.currency_code,
        amount=result.net_proceeds, transaction_date=termination_date,
        status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
        executed_by_user_id=user_id,
        description=f"Principal returned {result.principal_returned}, interest earned "
                    f"{result.interest_earned}, penalty {result.penalty}.",
    )
    db.add(transaction)

    if result.penalty > 0:
        db.add(InvestmentTransaction(
            investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
            transaction_type=InvestmentTransactionType.PENALTY, currency_code=investment.currency_code,
            amount=result.penalty, transaction_date=termination_date,
            status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
            executed_by_user_id=user_id,
        ))

    investment.principal_amount = max(Decimal(0), investment.principal_amount - amount)
    investment.status = (
        InvestmentStatus.TERMINATED if investment.principal_amount == 0
        else InvestmentStatus.PARTIALLY_TERMINATED
    )

    event_type = (
        InvestmentEventType.FULL_TERMINATION if is_full else InvestmentEventType.PARTIAL_TERMINATION
    )
    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=event_type,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"{'Full' if is_full else 'Partial'} early termination of {amount} "
                    f"{investment.currency_code}. Net proceeds {result.net_proceeds}.",
        amount=amount, currency_code=investment.currency_code,
        related_record_type="InvestmentTransaction", related_record_id=str(transaction.id),
        created_by_user_id=user_id,
    ))
    await db.flush()
    return transaction, result


async def rollover_investment(
    db: AsyncSession, original: Investment, rollover_amount: Decimal, new_rate: Decimal,
    new_start_date: datetime.date, new_maturity_date: datetime.date, new_reference: str, user_id,
) -> Investment:
    """
    SECTION 16: creates a genuinely NEW Investment row for the rolled
    portion - the original's own rate/maturity/terms are never
    overwritten. Explicit lineage via previous_investment_id /
    rolled_to_investment_id. A full rollover (rollover_amount ==
    outstanding principal) closes the original as ROLLED_OVER; a
    partial rollover reduces the original's outstanding principal and
    leaves it ACTIVE for the remainder.

    Idempotent by construction: this is only ever invoked once per
    rollover request by the API endpoint, which first re-validates the
    original's outstanding principal under its own row lock - a second,
    concurrent rollover request attempting to roll over the same
    already-reduced/closed original will fail that re-validation before
    this function is ever called, so it can never create two replacement
    investments from one origin.
    """
    is_full = rollover_amount >= original.principal_amount

    tenor_days = (new_maturity_date - new_start_date).days
    new_investment = Investment(
        investment_reference=new_reference, investment_type_code=original.investment_type_code,
        legal_entity_id=original.legal_entity_id, business_unit_id=original.business_unit_id,
        institution_id=original.institution_id, source_account_id=original.source_account_id,
        destination_account_id=original.destination_account_id, currency_code=original.currency_code,
        principal_amount=rollover_amount, original_principal_amount=rollover_amount,
        placement_date=new_start_date, value_date=new_start_date, start_date=new_start_date,
        maturity_date=new_maturity_date, tenor_days=tenor_days, interest_rate=new_rate,
        rate_type=original.rate_type, day_count_convention=original.day_count_convention,
        interest_payment_method=original.interest_payment_method,
        interest_payment_frequency=original.interest_payment_frequency,
        early_termination_allowed=original.early_termination_allowed,
        partial_termination_allowed=original.partial_termination_allowed,
        rollover_allowed=original.rollover_allowed, penalty_type=original.penalty_type,
        penalty_rate=original.penalty_rate, penalty_amount=original.penalty_amount,
        status=InvestmentStatus.ACTIVE, previous_investment_id=original.id,
        created_by_user_id=user_id, approved_by_user_id=user_id, placed_by_user_id=user_id,
    )
    db.add(new_investment)
    await db.flush()
    await record_initial_version(db, new_investment, user_id)

    db.add(InvestmentTransaction(
        investment_id=new_investment.id, legal_entity_id=new_investment.legal_entity_id,
        transaction_type=InvestmentTransactionType.PLACEMENT, currency_code=new_investment.currency_code,
        amount=rollover_amount, transaction_date=new_start_date,
        status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
        executed_by_user_id=user_id, related_investment_id=original.id,
        description=f"Rollover placement from {original.investment_reference}.",
    ))

    original.rolled_to_investment_id = new_investment.id
    original.principal_amount = max(Decimal(0), original.principal_amount - rollover_amount)
    original.status = (
        InvestmentStatus.ROLLED_OVER if is_full else InvestmentStatus.PARTIALLY_TERMINATED
    )

    db.add(InvestmentTransaction(
        investment_id=original.id, legal_entity_id=original.legal_entity_id,
        transaction_type=InvestmentTransactionType.ROLLOVER, currency_code=original.currency_code,
        amount=rollover_amount, transaction_date=new_start_date,
        status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
        executed_by_user_id=user_id, related_investment_id=new_investment.id,
        description=f"Rolled over to {new_investment.investment_reference}.",
    ))
    db.add(InvestmentEvent(
        investment_id=original.id, event_type=InvestmentEventType.ROLLOVER,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"{'Full' if is_full else 'Partial'} rollover of {rollover_amount} "
                    f"{original.currency_code} to {new_investment.investment_reference}.",
        amount=rollover_amount, currency_code=original.currency_code,
        related_record_type="Investment", related_record_id=str(new_investment.id),
        created_by_user_id=user_id,
    ))
    await db.flush()
    return new_investment


async def rebook_investment(
    db: AsyncSession, investment: Investment, new_rate: Decimal | None,
    new_maturity_date: datetime.date | None, additional_principal: Decimal, reason: str, user_id,
) -> Investment:
    """
    SECTION 17: rebooking amends the SAME investment's terms (unlike
    rollover, which creates a new investment record) - implemented as a
    new InvestmentVersion, never a silent overwrite of history. Any
    additional principal is recorded as its own REBOOKING transaction so
    the cash impact is traceable, exactly like every other financial
    event in this module.
    """
    changes: dict = {}
    if new_rate is not None:
        changes["interest_rate"] = new_rate
    if new_maturity_date is not None:
        changes["maturity_date"] = new_maturity_date
        changes["tenor_days"] = (new_maturity_date - investment.start_date).days
    if additional_principal:
        changes["principal_amount"] = investment.principal_amount + additional_principal
        changes["original_principal_amount"] = investment.original_principal_amount + additional_principal

    await apply_investment_update(db, investment, changes, reason, user_id)

    if additional_principal:
        db.add(InvestmentTransaction(
            investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
            transaction_type=InvestmentTransactionType.REBOOKING, currency_code=investment.currency_code,
            amount=additional_principal, transaction_date=datetime.date.today(),
            status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
            executed_by_user_id=user_id, description=reason,
        ))

    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=InvestmentEventType.REBOOKED,
        event_date=datetime.datetime.now(datetime.UTC), description=reason,
        amount=additional_principal or None, currency_code=investment.currency_code,
        created_by_user_id=user_id,
    ))
    await db.flush()
    return investment
