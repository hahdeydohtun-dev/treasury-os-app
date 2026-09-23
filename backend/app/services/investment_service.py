"""
Investment orchestration service: lifecycle transitions, versioning on
update, placement, termination (full/partial), rollover, rebooking, and
maturity settlement. Pure calculation lives in investment_engine.py;
this module handles persistence + audit trail, mirroring
app/services/facility_service.py.

Cash integration (Stage 4 financial-integrity hardening): every
lifecycle event that changes liquidity creates exactly one
TreasuryTransaction - the SAME single cash ledger every other Treasury
OS module feeds (app/models/treasury_transaction.py) - never a second,
parallel cash ledger. `InvestmentTransaction.cash_transaction_id` links
the investment-side record to its cash-side record. A rollover is
economically net-zero external cash movement (the same money leaves one
investment and re-enters a new one) - it gets a single NON_CASH
TreasuryTransaction purely for traceability, which the forecast engine
and cash-position calculations both already exclude from actual cash
flow (CashDirection.NON_CASH), so it can never double-count.

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
from app.models.lookup import CashDirection
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.services.investment_engine import calculate_early_termination, calculate_simple_interest


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


def recompute_expected_interest(investment: Investment) -> Decimal:
    """
    SECTION 5/10: `expected_interest` is always the CURRENT projection
    under CURRENT terms (current outstanding principal, current rate,
    current maturity date) - never a stale figure left over from before
    a partial termination, rate change, or maturity-date change. This is
    a pure recomputation from the investment's current fields; it never
    touches InvestmentVersion history (SECTION 5: "do not modify
    historical InvestmentVersion records").
    """
    return calculate_simple_interest(
        investment.principal_amount, investment.interest_rate, investment.start_date,
        investment.maturity_date, investment.day_count_convention,
    )


async def _create_cash_transaction(
    db: AsyncSession, investment: Investment, direction: CashDirection, event_type_code: str,
    amount: Decimal, transaction_date: datetime.date, bank_account_id, user_id,
    reference: str | None = None, narration: str | None = None,
) -> TreasuryTransaction:
    """
    Creates exactly one row in the single canonical cash ledger
    (TreasuryTransaction) for an investment lifecycle event. This is the
    ONLY place Stage 4 writes to that table - every investment cash
    effect goes through here, so there is exactly one cash-ledger entry
    per real (or, for NON_CASH, per traceable-but-zero-effect) event.
    """
    cash_txn = TreasuryTransaction(
        legal_entity_id=investment.legal_entity_id, event_type_code=event_type_code,
        direction=direction, event_date=transaction_date, value_date=transaction_date,
        transaction_currency_code=investment.currency_code, transaction_amount=amount,
        bank_account_id=bank_account_id, reference=reference, narration=narration,
        status=TransactionStatus.POSTED, source_type="INVESTMENT",
        source_record_id=str(investment.id), created_by_user_id=user_id,
    )
    db.add(cash_txn)
    await db.flush()
    return cash_txn


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

    # SECTION 5: a rate or maturity-date change must immediately update
    # the CURRENT projection - never left stale until some other event
    # happens to touch it.
    investment.expected_interest = recompute_expected_interest(investment)

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


async def place_investment(
    db: AsyncSession, investment: Investment, user_id, bank_account_id=None,
) -> InvestmentTransaction:
    """
    SECTION 2/11/12/39: idempotent (a PLACEMENT transaction already
    existing for this investment raises 409, backed at the database
    level by ux_investment_transactions_one_placement) and creates
    exactly one linked cash movement in the single canonical
    TreasuryTransaction ledger - a real OUTFLOW, never merely displayed.
    Cash sufficiency is validated by the caller (the API endpoint) using
    the existing cash-position architecture BEFORE this is invoked; this
    function focuses on atomically creating both records together.
    """
    existing_stmt = select(InvestmentTransaction).where(
        InvestmentTransaction.investment_id == investment.id,
        InvestmentTransaction.transaction_type == InvestmentTransactionType.PLACEMENT,
    )
    existing = (await db.execute(existing_stmt)).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="This investment has already been placed.")

    placement_date = investment.placement_date or datetime.date.today()
    cash_txn = await _create_cash_transaction(
        db, investment, CashDirection.OUTFLOW, "INVESTMENT_PLACEMENT", investment.principal_amount,
        placement_date, bank_account_id or investment.source_account_id, user_id,
        reference=investment.investment_reference,
        narration=f"Fixed deposit placement - {investment.investment_reference}",
    )

    transaction = InvestmentTransaction(
        investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
        transaction_type=InvestmentTransactionType.PLACEMENT, currency_code=investment.currency_code,
        amount=investment.principal_amount, transaction_date=placement_date,
        status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
        executed_by_user_id=user_id, cash_transaction_id=cash_txn.id,
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
    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=InvestmentEventType.STATUS_CHANGED,
        event_date=datetime.datetime.now(datetime.UTC), description="Investment is now ACTIVE.",
        previous_value={"status": InvestmentStatus.PLACEMENT_PENDING.value},
        new_value={"status": InvestmentStatus.ACTIVE.value}, created_by_user_id=user_id,
    ))
    await db.flush()
    return transaction


async def terminate_investment(
    db: AsyncSession, investment: Investment, amount: Decimal, termination_date: datetime.date,
    user_id, destination_account_id=None,
) -> tuple:
    """
    SECTION 3/5/15/39: reduces principal_amount, creates the termination
    InvestmentTransaction, creates exactly one linked INFLOW
    TreasuryTransaction whose amount reconciles EXACTLY to net_proceeds
    (never principal or interest alone - SECTION 3), and recomputes
    expected_interest on the remaining outstanding balance under
    current terms (SECTION 5) - never left stale.
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

    account_for_proceeds = destination_account_id or investment.destination_account_id or investment.source_account_id
    cash_txn = await _create_cash_transaction(
        db, investment, CashDirection.INFLOW, "INVESTMENT_TERMINATION", result.net_proceeds,
        termination_date, account_for_proceeds, user_id, reference=investment.investment_reference,
        narration=f"{'Full' if is_full else 'Partial'} early termination proceeds - "
                  f"{investment.investment_reference} (principal {result.principal_returned}, "
                  f"interest {result.interest_earned}, penalty {result.penalty})",
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
        executed_by_user_id=user_id, cash_transaction_id=cash_txn.id,
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
            description="Penalty component of the net proceeds above - not a separate cash "
                        "movement (already netted into the termination's single cash transaction).",
        ))

    investment.principal_amount = max(Decimal(0), investment.principal_amount - amount)
    investment.status = (
        InvestmentStatus.TERMINATED if investment.principal_amount == 0
        else InvestmentStatus.PARTIALLY_TERMINATED
    )
    # SECTION 5/10: recompute the CURRENT projected interest on whatever
    # principal remains outstanding - never left stale at the pre-
    # termination figure.
    investment.expected_interest = recompute_expected_interest(investment)

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


async def settle_maturity(
    db: AsyncSession, investment: Investment, settlement_date: datetime.date, user_id,
    destination_account_id=None, actual_interest: Decimal | None = None,
) -> InvestmentTransaction:
    """
    SECTION 4: an EXPLICIT, deliberate action - the maturity date passing
    on its own does nothing (never automatic). Settles the full
    outstanding principal + interest as one MATURITY_SETTLEMENT
    InvestmentTransaction with a linked INFLOW TreasuryTransaction, and
    moves the investment to MATURED. `actual_interest` lets the caller
    record the actually-received interest if it differs from the
    projected `expected_interest` (e.g. a variable-rate true-up); when
    omitted, the current expected_interest (already kept fresh by
    recompute_expected_interest) is used.
    """
    existing_stmt = select(InvestmentTransaction).where(
        InvestmentTransaction.investment_id == investment.id,
        InvestmentTransaction.transaction_type == InvestmentTransactionType.MATURITY_SETTLEMENT,
    )
    existing = (await db.execute(existing_stmt)).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="This investment has already been settled at maturity.")

    principal = investment.principal_amount
    interest = actual_interest if actual_interest is not None else investment.expected_interest
    total_proceeds = principal + interest

    account_for_proceeds = destination_account_id or investment.destination_account_id or investment.source_account_id
    cash_txn = await _create_cash_transaction(
        db, investment, CashDirection.INFLOW, "INVESTMENT_MATURITY", total_proceeds,
        settlement_date, account_for_proceeds, user_id, reference=investment.investment_reference,
        narration=f"Maturity settlement - {investment.investment_reference} "
                  f"(principal {principal}, interest {interest})",
    )

    transaction = InvestmentTransaction(
        investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
        transaction_type=InvestmentTransactionType.MATURITY_SETTLEMENT,
        currency_code=investment.currency_code, amount=total_proceeds, transaction_date=settlement_date,
        status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
        executed_by_user_id=user_id, cash_transaction_id=cash_txn.id,
        description=f"Principal {principal} + interest {interest} settled at maturity.",
    )
    db.add(transaction)

    investment.received_interest = (investment.received_interest or Decimal(0)) + interest
    investment.principal_amount = Decimal(0)
    investment.status = InvestmentStatus.MATURED

    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=InvestmentEventType.MATURED,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"Matured and settled: principal {principal} + interest {interest} "
                    f"= {total_proceeds}.",
        amount=total_proceeds, currency_code=investment.currency_code,
        related_record_type="InvestmentTransaction", related_record_id=str(transaction.id),
        created_by_user_id=user_id,
    ))
    await db.flush()
    return transaction


async def rollover_investment(
    db: AsyncSession, original: Investment, rollover_amount: Decimal, new_rate: Decimal,
    new_start_date: datetime.date, new_maturity_date: datetime.date, new_reference: str, user_id,
) -> Investment:
    """
    SECTION 7/16/39: creates a genuinely NEW Investment row for the
    rolled portion - the original's own rate/maturity/terms are never
    overwritten. Explicit lineage via previous_investment_id /
    rolled_to_investment_id.

    Cash treatment (SECTION 7): a rollover is economically net-zero
    external cash movement - the same money leaves one investment and
    re-enters a new one, so this does NOT create two separate offsetting
    OUTFLOW/INFLOW TreasuryTransactions (which would each show up as a
    large, unrelated-looking liquidity event). Instead it creates exactly
    ONE NON_CASH TreasuryTransaction, referenced by both the original's
    ROLLOVER InvestmentTransaction and the new investment's PLACEMENT
    InvestmentTransaction, purely for traceability - CashDirection.NON_CASH
    is already excluded from cash-position and forecast net-flow
    calculations elsewhere in the codebase, so this can never be
    double-counted as real liquidity.

    Idempotent by construction: this is only ever invoked once per
    rollover request by the API endpoint, which first re-validates the
    original's outstanding principal under its own row lock - a second,
    concurrent rollover request attempting to roll over the same
    already-reduced/closed original will fail that re-validation before
    this function is ever called, so it can never create two replacement
    investments from one origin.
    """
    is_full = rollover_amount >= original.principal_amount

    cash_txn = await _create_cash_transaction(
        db, original, CashDirection.NON_CASH, "INVESTMENT_ROLLOVER", rollover_amount,
        new_start_date, original.source_account_id, user_id, reference=new_reference,
        narration=f"Rollover of {original.investment_reference} into {new_reference} "
                  f"- internal, no net external cash movement.",
    )

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
        executed_by_user_id=user_id, related_investment_id=original.id, cash_transaction_id=cash_txn.id,
        description=f"Rollover placement from {original.investment_reference}.",
    ))

    original.rolled_to_investment_id = new_investment.id
    original.principal_amount = max(Decimal(0), original.principal_amount - rollover_amount)
    original.status = (
        InvestmentStatus.ROLLED_OVER if is_full else InvestmentStatus.PARTIALLY_TERMINATED
    )
    # SECTION 5/10: the remaining (unrolled) portion of the original, if
    # any, gets its expected interest recomputed on its own current
    # outstanding principal.
    original.expected_interest = recompute_expected_interest(original)

    db.add(InvestmentTransaction(
        investment_id=original.id, legal_entity_id=original.legal_entity_id,
        transaction_type=InvestmentTransactionType.ROLLOVER, currency_code=original.currency_code,
        amount=rollover_amount, transaction_date=new_start_date,
        status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
        executed_by_user_id=user_id, related_investment_id=new_investment.id, cash_transaction_id=cash_txn.id,
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
    bank_account_id=None, idempotency_key: str | None = None,
) -> Investment:
    """
    SECTION 6/17: rebooking amends the SAME investment's terms (unlike
    rollover, which creates a new investment record) - implemented as a
    new InvestmentVersion, never a silent overwrite of history. Any
    additional principal creates a real linked OUTFLOW TreasuryTransaction
    (the same cash-impact treatment as a placement), so a 50m -> 60m
    rebooking's 10m top-up is reflected in the cash ledger exactly like
    the spec's worked example requires - never silently absorbed into
    the new 60m figure without a corresponding cash movement.

    SECTION 2 (final freeze patch): idempotency here covers the ENTIRE
    rebooking operation - additional principal, rate changes, maturity
    changes, or any combination - not merely the cash-outflow path. The
    check is against `InvestmentEvent` (event_type=REBOOKED,
    reference=idempotency_key), because an InvestmentEvent is the ONE
    thing every successful rebooking call unconditionally creates,
    regardless of whether additional_principal is zero. An earlier
    version of this function checked InvestmentTransaction instead, which
    is only created when additional_principal > 0 - a rate-only or
    maturity-only rebooking retried with the same key would have slipped
    through undetected and applied the change a second time (a genuine
    bug, caught and fixed in this pass). The check runs BEFORE any
    mutation (no apply_investment_update, no version, no transaction, no
    event) so a detected retry is a true no-op - the investment is
    returned completely unchanged.
    """
    if idempotency_key is not None:
        existing_stmt = select(InvestmentEvent).where(
            InvestmentEvent.investment_id == investment.id,
            InvestmentEvent.event_type == InvestmentEventType.REBOOKED,
            InvestmentEvent.reference == idempotency_key,
        )
        existing = (await db.execute(existing_stmt)).scalars().first()
        if existing is not None:
            return investment

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
        cash_txn = await _create_cash_transaction(
            db, investment, CashDirection.OUTFLOW, "INVESTMENT_PLACEMENT", additional_principal,
            datetime.date.today(), bank_account_id or investment.source_account_id, user_id,
            reference=investment.investment_reference,
            narration=f"Additional principal funding - {investment.investment_reference}: {reason}",
        )
        db.add(InvestmentTransaction(
            investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
            transaction_type=InvestmentTransactionType.REBOOKING, currency_code=investment.currency_code,
            amount=additional_principal, transaction_date=datetime.date.today(),
            status=InvestmentTransactionStatus.EXECUTED, created_by_user_id=user_id,
            executed_by_user_id=user_id, cash_transaction_id=cash_txn.id, description=reason,
            idempotency_key=idempotency_key,
        ))

    db.add(InvestmentEvent(
        investment_id=investment.id, event_type=InvestmentEventType.REBOOKED,
        event_date=datetime.datetime.now(datetime.UTC), description=reason,
        amount=additional_principal or None, currency_code=investment.currency_code,
        reference=idempotency_key, created_by_user_id=user_id,
    ))
    await db.flush()
    return investment
