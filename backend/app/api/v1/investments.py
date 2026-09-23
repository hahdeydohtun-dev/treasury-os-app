import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    apply_resolved_entity_scope,
    assert_entity_access,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.banking import BankAccount
from app.models.investment import (
    Investment,
    InvestmentEvent,
    InvestmentStatus,
    InvestmentTransaction,
    InvestmentType,
    InvestmentVersion,
)
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.investment import (
    InvestmentCreate,
    InvestmentEventOut,
    InvestmentOut,
    InvestmentTransactionOut,
    InvestmentTypeOut,
    InvestmentUpdate,
    InvestmentVersionOut,
    MaturitySettlementRequest,
    PlacementRequest,
    RebookingRequest,
    RolloverComparisonOut,
    RolloverComparisonRequest,
    RolloverRequest,
    StatusChangeRequest,
    TerminationRequest,
    TerminationResultOut,
)
from app.services.audit_service import record_audit_event
from app.services.cash_position_service import calculate_operational_available_cash
from app.services.investment_engine import calculate_simple_interest, compare_rollover_options
from app.services.investment_service import (
    apply_investment_update,
    load_investment_for_update,
    place_investment,
    rebook_investment,
    record_initial_version,
    rollover_investment,
    settle_maturity,
    terminate_investment,
    transition_investment_status,
)
from app.services.investment_validation_service import (
    validate_placement,
    validate_termination_amount,
)

router = APIRouter(tags=["investments"])

MODULE = TreasuryModule.INVESTMENTS


async def _load_investment(db: AsyncSession, investment_id: uuid.UUID) -> Investment:
    investment = await db.get(Investment, investment_id)
    if investment is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    return investment


async def _assert_investment_scope(db: AsyncSession, user: User, investment: Investment, action) -> None:
    await assert_entity_access(db, user, MODULE, action, investment.legal_entity_id)


def _expected_interest(investment: Investment) -> Decimal:
    return calculate_simple_interest(
        investment.principal_amount, investment.interest_rate, investment.start_date,
        investment.maturity_date, investment.day_count_convention,
    )


@router.get("/investment-types", response_model=list[InvestmentTypeOut])
async def list_investment_types(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(select(InvestmentType).where(InvestmentType.is_active.is_(True)))
    return list(result.scalars().all())


@router.post("/investments", response_model=InvestmentOut, status_code=status.HTTP_201_CREATED)
async def create_investment(
    payload: InvestmentCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Investment:
    await assert_entity_access(db, user, MODULE, TreasuryAction.CREATE, payload.legal_entity_id)

    if payload.principal_amount <= 0:
        raise HTTPException(status_code=400, detail="Principal amount must be positive.")
    if payload.maturity_date < payload.start_date:
        raise HTTPException(status_code=400, detail="Maturity date cannot be before the start date.")

    tenor_days = (payload.maturity_date - payload.start_date).days
    investment = Investment(
        **payload.model_dump(), original_principal_amount=payload.principal_amount,
        tenor_days=tenor_days, status=InvestmentStatus.DRAFT, created_by_user_id=user.id,
    )
    investment.expected_interest = _expected_interest(investment)
    db.add(investment)
    await db.flush()
    await record_initial_version(db, investment, user.id)
    await record_audit_event(
        db, module=MODULE.value, action="CREATE", record_type="Investment",
        record_id=str(investment.id), user_id=user.id, legal_entity_id=investment.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(investment)
    return investment


@router.get("/investments", response_model=list[InvestmentOut])
async def list_investments(
    legal_entity_id: uuid.UUID | None = None,
    status_filter: InvestmentStatus | None = None,
    institution_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No INVESTMENTS:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(Investment).where(Investment.is_active.is_(True))
    stmt = apply_resolved_entity_scope(stmt, Investment.legal_entity_id, resolved_ids)
    if legal_entity_id:
        stmt = stmt.where(Investment.legal_entity_id == legal_entity_id)
    if status_filter:
        stmt = stmt.where(Investment.status == status_filter)
    if institution_id:
        stmt = stmt.where(Investment.institution_id == institution_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/investments/{investment_id}", response_model=InvestmentOut)
async def get_investment(
    investment_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Investment:
    investment = await _load_investment(db, investment_id)
    await _assert_investment_scope(db, user, investment, TreasuryAction.VIEW)
    return investment


@router.patch("/investments/{investment_id}", response_model=InvestmentOut)
async def update_investment(
    investment_id: uuid.UUID, payload: InvestmentUpdate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Investment:
    investment = await load_investment_for_update(db, investment_id)
    if investment is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    await _assert_investment_scope(db, user, investment, TreasuryAction.EDIT)
    if investment.status != InvestmentStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only a DRAFT investment can be freely edited.")

    changes = payload.model_dump(exclude={"change_reason"}, exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes provided.")
    investment = await apply_investment_update(db, investment, changes, payload.change_reason, user.id)
    investment.expected_interest = _expected_interest(investment)
    await record_audit_event(
        db, module=MODULE.value, action="UPDATE", record_type="Investment",
        record_id=str(investment.id), user_id=user.id, legal_entity_id=investment.legal_entity_id,
        reason=payload.change_reason,
    )
    await db.commit()
    await db.refresh(investment)
    return investment


@router.patch("/investments/{investment_id}/status", response_model=InvestmentOut)
async def change_investment_status(
    investment_id: uuid.UUID, payload: StatusChangeRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Investment:
    """SECTION 8/9: server-side-validated lifecycle transition, e.g. DRAFT -> SUBMITTED
    -> UNDER_REVIEW -> APPROVED. Distinguishes the approval decision from actual placement."""
    investment = await load_investment_for_update(db, investment_id)
    if investment is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    action = TreasuryAction.APPROVE if payload.new_status == InvestmentStatus.APPROVED else TreasuryAction.SUBMIT
    await _assert_investment_scope(db, user, investment, action)

    previous_status = investment.status.value
    investment = await transition_investment_status(db, investment, payload.new_status, payload.reason, user.id)
    await record_audit_event(
        db, module=MODULE.value, action="STATUS_CHANGE", record_type="Investment",
        record_id=str(investment.id), user_id=user.id, legal_entity_id=investment.legal_entity_id,
        reason=payload.reason, previous_value={"status": previous_status},
        new_value={"status": investment.status.value},
    )
    await db.commit()
    await db.refresh(investment)
    return investment


@router.post("/investments/{investment_id}/place", response_model=InvestmentOut)
async def place_investment_endpoint(
    investment_id: uuid.UUID, payload: PlacementRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Investment:
    """
    SECTION 2/11/12/39: row-locks the investment (idempotency - a
    concurrent second placement call serializes here, then sees the
    already-placed status/existing PLACEMENT transaction and is
    rejected) and validates before mutating. Available cash is
    determined AUTHORITATIVELY from the existing cash-position
    architecture (app/services/cash_position_service.py, itself built on
    BankBalance - the same source every other cash figure in this
    application uses) - a client-supplied `available_cash` is accepted
    only for display/testing and is NEVER the basis for the accept/
    reject decision.
    """
    investment = await load_investment_for_update(db, investment_id)
    if investment is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    await _assert_investment_scope(db, user, investment, TreasuryAction.EXECUTE)

    investment.placement_date = payload.placement_date
    investment.value_date = payload.value_date or payload.placement_date

    source_entity_id = None
    authoritative_available_cash = None
    if investment.source_account_id is not None:
        account = await db.get(BankAccount, investment.source_account_id)
        if account is None:
            raise HTTPException(status_code=400, detail="Source account not found.")
        source_entity_id = account.legal_entity_id
        # SECTION 12: currency integrity - never silently convert.
        if account.currency_code != investment.currency_code:
            raise HTTPException(
                status_code=400,
                detail=f"Source account currency {account.currency_code} does not match the "
                       f"investment's currency {investment.currency_code}. Treasury OS does not "
                       "support an implicit FX conversion for this flow.",
            )
        capacity = await calculate_operational_available_cash(db, bank_account_id=account.id)
        authoritative_available_cash = capacity.operational_available_cash

    reasons = validate_placement(investment, source_entity_id, authoritative_available_cash)
    if reasons:
        raise HTTPException(status_code=400, detail={"validation_errors": reasons})

    await place_investment(db, investment, user.id)
    await record_audit_event(
        db, module=MODULE.value, action="PLACE", record_type="Investment",
        record_id=str(investment.id), user_id=user.id, legal_entity_id=investment.legal_entity_id,
        new_value={"principal_amount": str(investment.principal_amount)},
    )
    await db.commit()
    await db.refresh(investment)
    return investment


@router.post("/investments/{investment_id}/terminate", response_model=TerminationResultOut)
async def terminate_investment_endpoint(
    investment_id: uuid.UUID, payload: TerminationRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TerminationResultOut:
    """
    SECTION 14/15/39: row-locked re-validation against the CURRENT
    outstanding principal - two concurrent partial terminations racing
    against the same investment can never together exceed the
    outstanding principal, because the second request blocks on the
    lock until the first commits, then is re-validated against the
    already-reduced balance.
    """
    investment = await load_investment_for_update(db, investment_id)
    if investment is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    await _assert_investment_scope(db, user, investment, TreasuryAction.TERMINATE)

    if investment.status not in (InvestmentStatus.ACTIVE, InvestmentStatus.PARTIALLY_TERMINATED):
        raise HTTPException(
            status_code=400,
            detail=f"Investment must be ACTIVE or PARTIALLY_TERMINATED to terminate "
                   f"(current status: {investment.status.value}).",
        )
    if not investment.early_termination_allowed:
        raise HTTPException(status_code=400, detail="Early termination is not permitted for this investment.")

    is_partial = payload.amount < investment.principal_amount
    if is_partial and not investment.partial_termination_allowed:
        raise HTTPException(status_code=400, detail="Partial early termination is not permitted for this investment.")

    reasons = validate_termination_amount(
        investment.principal_amount, payload.amount, investment.partial_termination_allowed,
    )
    if reasons:
        raise HTTPException(status_code=409, detail={"validation_errors": reasons})

    if payload.destination_account_id is not None:
        account = await db.get(BankAccount, payload.destination_account_id)
        if account is None:
            raise HTTPException(status_code=400, detail="Destination account not found.")
        if account.legal_entity_id != investment.legal_entity_id:
            raise HTTPException(
                status_code=400, detail="Destination account does not belong to this investment's entity.",
            )
        if account.currency_code != investment.currency_code:
            raise HTTPException(
                status_code=400,
                detail=f"Destination account currency {account.currency_code} does not match the "
                       f"investment's currency {investment.currency_code}.",
            )

    _transaction, result = await terminate_investment(
        db, investment, payload.amount, payload.termination_date, user.id,
        destination_account_id=payload.destination_account_id,
    )
    await record_audit_event(
        db, module=MODULE.value, action="TERMINATE", record_type="Investment",
        record_id=str(investment.id), user_id=user.id, legal_entity_id=investment.legal_entity_id,
        new_value={"amount": str(payload.amount), "net_proceeds": str(result.net_proceeds)},
    )
    await db.commit()
    await db.refresh(investment)
    return TerminationResultOut(
        principal_returned=result.principal_returned, interest_earned=result.interest_earned,
        interest_forfeited=result.interest_forfeited, penalty=result.penalty,
        net_proceeds=result.net_proceeds, investment=InvestmentOut.model_validate(investment),
    )


@router.post("/investments/{investment_id}/settle-maturity", response_model=InvestmentOut)
async def settle_maturity_endpoint(
    investment_id: uuid.UUID, payload: MaturitySettlementRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Investment:
    """
    SECTION 4: an explicit, deliberate settlement action - the maturity
    date passing never automatically creates this. Idempotent: a second
    settlement attempt sees the already-existing MATURITY_SETTLEMENT
    transaction (checked under the row lock) and is rejected (409).
    """
    investment = await load_investment_for_update(db, investment_id)
    if investment is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    await _assert_investment_scope(db, user, investment, TreasuryAction.EXECUTE)

    if investment.status not in (InvestmentStatus.ACTIVE, InvestmentStatus.PARTIALLY_TERMINATED):
        raise HTTPException(
            status_code=400,
            detail=f"Investment must be ACTIVE or PARTIALLY_TERMINATED to settle at maturity "
                   f"(current status: {investment.status.value}).",
        )
    if payload.settlement_date < investment.maturity_date:
        raise HTTPException(
            status_code=400,
            detail=f"Settlement date {payload.settlement_date} is before the investment's "
                   f"maturity date {investment.maturity_date}.",
        )

    if payload.destination_account_id is not None:
        account = await db.get(BankAccount, payload.destination_account_id)
        if account is None:
            raise HTTPException(status_code=400, detail="Destination account not found.")
        if account.legal_entity_id != investment.legal_entity_id:
            raise HTTPException(
                status_code=400, detail="Destination account does not belong to this investment's entity.",
            )
        if account.currency_code != investment.currency_code:
            raise HTTPException(
                status_code=400,
                detail=f"Destination account currency {account.currency_code} does not match the "
                       f"investment's currency {investment.currency_code}.",
            )

    await settle_maturity(
        db, investment, payload.settlement_date, user.id,
        destination_account_id=payload.destination_account_id, actual_interest=payload.actual_interest,
    )
    await record_audit_event(
        db, module=MODULE.value, action="SETTLE_MATURITY", record_type="Investment",
        record_id=str(investment.id), user_id=user.id, legal_entity_id=investment.legal_entity_id,
    )
    await db.commit()
    await db.refresh(investment)
    return investment


@router.post("/investments/{investment_id}/rollover", response_model=InvestmentOut, status_code=201)
async def rollover_investment_endpoint(
    investment_id: uuid.UUID, payload: RolloverRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Investment:
    """
    SECTION 16/39: row-locked re-validation - a concurrent second
    rollover request against the same (now already rolled-over/reduced)
    investment is rejected before it can create a second replacement
    investment.
    """
    investment = await load_investment_for_update(db, investment_id)
    if investment is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    await _assert_investment_scope(db, user, investment, TreasuryAction.ROLLOVER)

    if investment.status not in (InvestmentStatus.ACTIVE, InvestmentStatus.PARTIALLY_TERMINATED, InvestmentStatus.MATURED):
        raise HTTPException(status_code=400, detail=f"Cannot roll over an investment in status {investment.status.value}.")
    if not investment.rollover_allowed:
        raise HTTPException(status_code=400, detail="Rollover is not permitted for this investment.")
    if payload.rollover_amount <= 0 or payload.rollover_amount > investment.principal_amount:
        raise HTTPException(
            status_code=409,
            detail=f"Rollover amount {payload.rollover_amount} must be positive and not exceed the "
                   f"outstanding principal {investment.principal_amount}.",
        )

    new_investment = await rollover_investment(
        db, investment, payload.rollover_amount, payload.new_rate, payload.new_start_date,
        payload.new_maturity_date, payload.new_reference, user.id,
    )
    new_investment.expected_interest = _expected_interest(new_investment)
    await record_audit_event(
        db, module=MODULE.value, action="ROLLOVER", record_type="Investment",
        record_id=str(investment.id), user_id=user.id, legal_entity_id=investment.legal_entity_id,
        new_value={"rolled_to": str(new_investment.id), "amount": str(payload.rollover_amount)},
    )
    await db.commit()
    await db.refresh(new_investment)
    return new_investment


@router.post("/investments/{investment_id}/rebook", response_model=InvestmentOut)
async def rebook_investment_endpoint(
    investment_id: uuid.UUID, payload: RebookingRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Investment:
    investment = await load_investment_for_update(db, investment_id)
    if investment is None:
        raise HTTPException(status_code=404, detail="Investment not found")
    await _assert_investment_scope(db, user, investment, TreasuryAction.EDIT)

    if investment.status not in (InvestmentStatus.ACTIVE, InvestmentStatus.PARTIALLY_TERMINATED):
        raise HTTPException(status_code=400, detail=f"Cannot rebook an investment in status {investment.status.value}.")

    if payload.additional_principal and payload.additional_principal > 0:
        account_id = payload.bank_account_id or investment.source_account_id
        if account_id is not None:
            account = await db.get(BankAccount, account_id)
            if account is None:
                raise HTTPException(status_code=400, detail="Funding account not found.")
            if account.legal_entity_id != investment.legal_entity_id:
                raise HTTPException(
                    status_code=400, detail="Funding account does not belong to this investment's entity.",
                )
            if account.currency_code != investment.currency_code:
                raise HTTPException(
                    status_code=400,
                    detail=f"Funding account currency {account.currency_code} does not match the "
                           f"investment's currency {investment.currency_code}.",
                )
            capacity = await calculate_operational_available_cash(db, bank_account_id=account.id)
            if payload.additional_principal > capacity.operational_available_cash:
                raise HTTPException(
                    status_code=400,
                    detail=f"Additional principal {payload.additional_principal} exceeds available cash "
                           f"{capacity.operational_available_cash} in the funding account.",
                )

    investment = await rebook_investment(
        db, investment, payload.new_rate, payload.new_maturity_date, payload.additional_principal,
        payload.reason, user.id, bank_account_id=payload.bank_account_id,
        idempotency_key=payload.idempotency_key,
    )
    await record_audit_event(
        db, module=MODULE.value, action="REBOOK", record_type="Investment",
        record_id=str(investment.id), user_id=user.id, legal_entity_id=investment.legal_entity_id,
        reason=payload.reason,
    )
    await db.commit()
    await db.refresh(investment)
    return investment


@router.post("/investments/{investment_id}/compare-rollover", response_model=RolloverComparisonOut)
async def compare_rollover_endpoint(
    investment_id: uuid.UUID, payload: RolloverComparisonRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RolloverComparisonOut:
    """SECTION 17/25: transparent comparison factors only - never a ranked "best option"."""
    investment = await _load_investment(db, investment_id)
    await _assert_investment_scope(db, user, investment, TreasuryAction.VIEW)

    comparison = compare_rollover_options(
        current_principal=investment.principal_amount, current_rate=investment.interest_rate,
        current_start=investment.start_date, current_maturity=investment.maturity_date,
        current_convention=investment.day_count_convention, new_principal=payload.new_principal,
        new_rate=payload.new_rate, new_start=payload.new_start_date,
        new_maturity=payload.new_maturity_date, new_convention=investment.day_count_convention,
        penalty=payload.penalty,
    )
    return RolloverComparisonOut(
        current_rate=comparison.current_rate, proposed_rate=comparison.proposed_rate,
        current_maturity=comparison.current_maturity, proposed_maturity=comparison.proposed_maturity,
        principal=comparison.principal, additional_principal=comparison.additional_principal,
        withdrawn_principal=comparison.withdrawn_principal,
        expected_interest_current_term=comparison.expected_interest_current_term,
        expected_interest_new_term=comparison.expected_interest_new_term,
        penalty=comparison.penalty, net_expected_proceeds=comparison.net_expected_proceeds,
    )


@router.get("/investments/{investment_id}/versions", response_model=list[InvestmentVersionOut])
async def list_investment_versions(
    investment_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    investment = await _load_investment(db, investment_id)
    await _assert_investment_scope(db, user, investment, TreasuryAction.VIEW)
    result = await db.execute(
        select(InvestmentVersion).where(InvestmentVersion.investment_id == investment_id)
        .order_by(InvestmentVersion.version)
    )
    return list(result.scalars().all())


@router.get("/investments/{investment_id}/events", response_model=list[InvestmentEventOut])
async def list_investment_events(
    investment_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    investment = await _load_investment(db, investment_id)
    await _assert_investment_scope(db, user, investment, TreasuryAction.VIEW)
    result = await db.execute(
        select(InvestmentEvent).where(InvestmentEvent.investment_id == investment_id)
        .order_by(InvestmentEvent.event_date.desc())
    )
    return list(result.scalars().all())


@router.get("/investments/{investment_id}/transactions", response_model=list[InvestmentTransactionOut])
async def list_investment_transactions(
    investment_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    investment = await _load_investment(db, investment_id)
    await _assert_investment_scope(db, user, investment, TreasuryAction.VIEW)
    result = await db.execute(
        select(InvestmentTransaction).where(InvestmentTransaction.investment_id == investment_id)
        .order_by(InvestmentTransaction.transaction_date)
    )
    return list(result.scalars().all())
