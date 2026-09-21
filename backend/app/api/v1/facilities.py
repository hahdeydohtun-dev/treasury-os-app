import datetime
import uuid

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
from app.models.facility import (
    CovenantStatus,
    DrawdownStatus,
    Facility,
    FacilityCollateral,
    FacilityCovenant,
    FacilityDrawdown,
    FacilityEvent,
    FacilityEventType,
    FacilityFee,
    FacilityRepayment,
    FacilityStatus,
    FacilitySubLimit,
    FacilityType,
    FacilityVersion,
    RepaymentStatus,
)
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.facility import (
    CollateralCreate,
    CollateralOut,
    CovenantCreate,
    CovenantOut,
    CovenantUpdate,
    DrawdownCreate,
    DrawdownOut,
    FacilityCreate,
    FacilityEventOut,
    FacilityOut,
    FacilitySubLimitCreate,
    FacilitySubLimitOut,
    FacilityTypeOut,
    FacilityUpdate,
    FacilityUtilizationOut,
    FacilityVersionOut,
    FeeCreate,
    FeeOut,
    RepaymentCreate,
    RepaymentOut,
    RepaymentPaymentRequest,
    StatusChangeRequest,
)
from app.services.audit_service import record_audit_event
from app.services.drawdown_validation_service import validate_drawdown
from app.services.facility_engine import (
    calculate_utilization,
    effective_interest_rate,
    evaluate_covenant,
)
from app.services.facility_service import (
    apply_facility_update,
    execute_drawdown,
    load_drawdown_for_update,
    load_facility_for_update,
    load_repayment_for_update,
    load_sub_limit_for_update,
    record_initial_version,
    record_repayment_payment,
    transition_facility_status,
)

router = APIRouter(tags=["facilities"])

MODULE = TreasuryModule.FACILITIES


async def _load_facility(db: AsyncSession, facility_id: uuid.UUID) -> Facility:
    facility = await db.get(Facility, facility_id)
    if facility is None:
        raise HTTPException(status_code=404, detail="Facility not found")
    return facility


async def _assert_facility_scope(db: AsyncSession, user: User, facility: Facility, action) -> None:
    await assert_entity_access(db, user, MODULE, action, facility.legal_entity_id)


@router.get("/facility-types", response_model=list[FacilityTypeOut])
async def list_facility_types(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(select(FacilityType).where(FacilityType.is_active.is_(True)))
    return list(result.scalars().all())


@router.post("/facilities", response_model=FacilityOut, status_code=status.HTTP_201_CREATED)
async def create_facility(
    payload: FacilityCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Facility:
    await assert_entity_access(db, user, MODULE, TreasuryAction.CREATE, payload.legal_entity_id)

    facility = Facility(**payload.model_dump(), status=FacilityStatus.DRAFT, created_by_user_id=user.id)
    db.add(facility)
    await db.flush()
    await record_initial_version(db, facility, user.id)
    await record_audit_event(
        db, module=MODULE.value, action="CREATE", record_type="Facility",
        record_id=str(facility.id), user_id=user.id, legal_entity_id=facility.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(facility)
    return facility


@router.get("/facilities", response_model=list[FacilityOut])
async def list_facilities(
    legal_entity_id: uuid.UUID | None = None,
    status_filter: FacilityStatus | None = None,
    commitment_type: str | None = None,
    lender_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No FACILITIES:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(Facility).where(Facility.is_active.is_(True))
    stmt = apply_resolved_entity_scope(stmt, Facility.legal_entity_id, resolved_ids)
    if legal_entity_id:
        stmt = stmt.where(Facility.legal_entity_id == legal_entity_id)
    if status_filter:
        stmt = stmt.where(Facility.status == status_filter)
    if commitment_type:
        stmt = stmt.where(Facility.commitment_type == commitment_type)
    if lender_id:
        stmt = stmt.where(Facility.lender_id == lender_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/facilities/{facility_id}", response_model=FacilityOut)
async def get_facility(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Facility:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    return facility


@router.patch("/facilities/{facility_id}", response_model=FacilityOut)
async def update_facility(
    facility_id: uuid.UUID, payload: FacilityUpdate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Facility:
    # Locked: a concurrent drawdown/repayment execution against this same
    # facility serializes against a term change instead of racing it.
    facility = await load_facility_for_update(db, facility_id)
    if facility is None:
        raise HTTPException(status_code=404, detail="Facility not found")
    await _assert_facility_scope(db, user, facility, TreasuryAction.EDIT)

    changes = payload.model_dump(exclude={"change_reason"}, exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes provided.")
    previous = {"committed_limit": str(facility.committed_limit)}
    facility = await apply_facility_update(db, facility, changes, payload.change_reason, user.id)
    await record_audit_event(
        db, module=MODULE.value, action="UPDATE", record_type="Facility",
        record_id=str(facility.id), user_id=user.id, legal_entity_id=facility.legal_entity_id,
        reason=payload.change_reason, previous_value=previous,
        new_value={k: str(v) for k, v in changes.items()},
    )
    await db.commit()
    await db.refresh(facility)
    return facility


@router.patch("/facilities/{facility_id}/status", response_model=FacilityOut)
async def change_facility_status(
    facility_id: uuid.UUID, payload: StatusChangeRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Facility:
    # Locked: two concurrent status-change requests serialize, so the
    # second sees the already-updated status and is evaluated against
    # the correct current transition table entry.
    facility = await load_facility_for_update(db, facility_id)
    if facility is None:
        raise HTTPException(status_code=404, detail="Facility not found")
    await _assert_facility_scope(db, user, facility, TreasuryAction.APPROVE)

    previous_status = facility.status.value
    facility = await transition_facility_status(db, facility, payload.new_status, payload.reason, user.id)
    await record_audit_event(
        db, module=MODULE.value, action="STATUS_CHANGE", record_type="Facility",
        record_id=str(facility.id), user_id=user.id, legal_entity_id=facility.legal_entity_id,
        reason=payload.reason, previous_value={"status": previous_status},
        new_value={"status": facility.status.value},
    )
    await db.commit()
    await db.refresh(facility)
    return facility


@router.get("/facilities/{facility_id}/utilization", response_model=FacilityUtilizationOut)
async def get_facility_utilization(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityUtilizationOut:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    utilization = calculate_utilization(facility)
    return FacilityUtilizationOut(
        committed_limit=utilization.committed_limit, drawn_amount=utilization.drawn_amount,
        undrawn_amount=utilization.undrawn_amount,
        covenant_restricted_amount=utilization.covenant_restricted_amount,
        available_amount=utilization.available_amount, utilization_pct=utilization.utilization_pct,
        effective_interest_rate=effective_interest_rate(facility),
    )


@router.get("/facilities/{facility_id}/versions", response_model=list[FacilityVersionOut])
async def list_facility_versions(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    result = await db.execute(
        select(FacilityVersion).where(FacilityVersion.facility_id == facility_id)
        .order_by(FacilityVersion.version)
    )
    return list(result.scalars().all())


@router.post(
    "/facilities/{facility_id}/sub-limits", response_model=FacilitySubLimitOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_sub_limit(
    facility_id: uuid.UUID, payload: FacilitySubLimitCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilitySubLimit:
    """
    SECTION 15: an optional purpose-restricted sub-limit within a
    facility's total. A sub-limit's own remaining capacity is enforced
    independently of the facility's overall available amount by
    `validate_drawdown` whenever a drawdown references it.
    """
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.CONFIGURE)

    if payload.limit_amount > facility.committed_limit:
        raise HTTPException(
            status_code=400,
            detail="Sub-limit amount cannot exceed the facility's committed limit.",
        )

    sub_limit = FacilitySubLimit(facility_id=facility_id, **payload.model_dump())
    db.add(sub_limit)
    await db.flush()
    await record_audit_event(
        db, module=MODULE.value, action="SUB_LIMIT_CREATE", record_type="FacilitySubLimit",
        record_id=str(sub_limit.id), user_id=user.id, legal_entity_id=facility.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(sub_limit)
    return sub_limit


@router.get("/facilities/{facility_id}/sub-limits", response_model=list[FacilitySubLimitOut])
async def list_sub_limits(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    result = await db.execute(
        select(FacilitySubLimit).where(
            FacilitySubLimit.facility_id == facility_id, FacilitySubLimit.is_active.is_(True)
        )
    )
    return list(result.scalars().all())


@router.get("/facilities/{facility_id}/events", response_model=list[FacilityEventOut])
async def list_facility_events(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    result = await db.execute(
        select(FacilityEvent).where(FacilityEvent.facility_id == facility_id)
        .order_by(FacilityEvent.event_date.desc())
    )
    return list(result.scalars().all())


@router.post(
    "/facilities/{facility_id}/drawdowns", response_model=DrawdownOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_drawdown(
    facility_id: uuid.UUID, payload: DrawdownCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityDrawdown:
    facility = await _load_facility(db, facility_id)
    await assert_entity_access(db, user, MODULE, TreasuryAction.CREATE, payload.legal_entity_id)
    if facility.legal_entity_id != payload.legal_entity_id:
        raise HTTPException(status_code=400, detail="Entity does not match this facility.")

    sub_limit = None
    if payload.sub_limit_id is not None:
        sub_limit = await db.get(FacilitySubLimit, payload.sub_limit_id)
        if sub_limit is None or sub_limit.facility_id != facility_id:
            raise HTTPException(status_code=400, detail="Sub-limit does not belong to this facility.")

    reasons = validate_drawdown(
        facility, payload.drawdown_amount, payload.currency_code, payload.drawdown_date,
        sub_limit=sub_limit,
    )
    if reasons:
        raise HTTPException(status_code=400, detail={"validation_errors": reasons})

    drawdown = FacilityDrawdown(
        facility_id=facility_id, **payload.model_dump(),
        interest_rate_applicable=effective_interest_rate(facility),
        status=DrawdownStatus.SUBMITTED, created_by_user_id=user.id,
    )
    db.add(drawdown)
    await db.flush()
    await record_audit_event(
        db, module=MODULE.value, action="DRAWDOWN_CREATE", record_type="FacilityDrawdown",
        record_id=str(drawdown.id), user_id=user.id, legal_entity_id=drawdown.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(drawdown)
    return drawdown


@router.get("/facilities/{facility_id}/drawdowns", response_model=list[DrawdownOut])
async def list_drawdowns(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    result = await db.execute(
        select(FacilityDrawdown).where(FacilityDrawdown.facility_id == facility_id)
        .order_by(FacilityDrawdown.drawdown_date.desc())
    )
    return list(result.scalars().all())


@router.get("/drawdowns/{drawdown_id}", response_model=DrawdownOut)
async def get_drawdown(
    drawdown_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityDrawdown:
    drawdown = await db.get(FacilityDrawdown, drawdown_id)
    if drawdown is None:
        raise HTTPException(status_code=404, detail="Drawdown not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, drawdown.legal_entity_id)
    return drawdown


@router.post("/drawdowns/{drawdown_id}/approve", response_model=DrawdownOut)
async def approve_drawdown(
    drawdown_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityDrawdown:
    # Row-locked: two concurrent approve calls on the same drawdown serialize
    # here, so the second sees the already-APPROVED status and is rejected
    # rather than both succeeding (idempotency, SECTION 2).
    drawdown = await load_drawdown_for_update(db, drawdown_id)
    if drawdown is None:
        raise HTTPException(status_code=404, detail="Drawdown not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.APPROVE, drawdown.legal_entity_id)
    if drawdown.status != DrawdownStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail=f"Cannot approve a drawdown in status {drawdown.status.value}.")

    drawdown.status = DrawdownStatus.APPROVED
    drawdown.approved_by_user_id = user.id
    await record_audit_event(
        db, module=MODULE.value, action="DRAWDOWN_APPROVE", record_type="FacilityDrawdown",
        record_id=str(drawdown.id), user_id=user.id, legal_entity_id=drawdown.legal_entity_id,
    )
    await db.commit()
    await db.refresh(drawdown)
    return drawdown


@router.post("/drawdowns/{drawdown_id}/execute", response_model=DrawdownOut)
async def execute_drawdown_endpoint(
    drawdown_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityDrawdown:
    """
    SECTION 2/3: row-locks the drawdown AND its facility (and sub-limit,
    if any) before re-validating and mutating - this is what actually
    prevents two concurrent executions (of the same drawdown, or of two
    different drawdowns racing for the same facility's capacity) from
    ever producing an overdraw. A concurrent request blocks on the lock
    until the first commits, then sees the now-current (post-mutation)
    state and is correctly re-validated against it - never the stale
    state it originally read.
    """
    drawdown = await load_drawdown_for_update(db, drawdown_id)
    if drawdown is None:
        raise HTTPException(status_code=404, detail="Drawdown not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.EXECUTE, drawdown.legal_entity_id)
    if drawdown.status != DrawdownStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail=f"Only an APPROVED drawdown can be executed (current status: {drawdown.status.value}).",
        )

    facility = await load_facility_for_update(db, drawdown.facility_id)
    if facility is None:
        raise HTTPException(status_code=404, detail="Facility not found for this drawdown.")
    sub_limit = None
    if drawdown.sub_limit_id is not None:
        sub_limit = await load_sub_limit_for_update(db, drawdown.sub_limit_id)

    # Re-validate against the LOCKED, current state - capacity may have
    # been consumed by another drawdown approved and executed after this
    # one was approved but before it was executed.
    reasons = validate_drawdown(
        facility, drawdown.drawdown_amount, drawdown.currency_code, drawdown.drawdown_date,
        sub_limit=sub_limit,
    )
    if reasons:
        raise HTTPException(
            status_code=409,
            detail={
                "validation_errors": reasons,
                "message": "Facility or sub-limit capacity changed since this drawdown was "
                           "approved and can no longer accommodate it.",
            },
        )

    await execute_drawdown(db, facility, drawdown, user.id)
    await record_audit_event(
        db, module=MODULE.value, action="DRAWDOWN_EXECUTE", record_type="FacilityDrawdown",
        record_id=str(drawdown.id), user_id=user.id, legal_entity_id=drawdown.legal_entity_id,
        new_value={"drawdown_amount": str(drawdown.drawdown_amount)},
    )
    await db.commit()
    await db.refresh(drawdown)
    return drawdown


@router.post(
    "/facilities/{facility_id}/repayments", response_model=RepaymentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_repayment(
    facility_id: uuid.UUID, payload: RepaymentCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityRepayment:
    facility = await _load_facility(db, facility_id)
    await assert_entity_access(db, user, MODULE, TreasuryAction.CREATE, payload.legal_entity_id)
    if facility.legal_entity_id != payload.legal_entity_id:
        raise HTTPException(status_code=400, detail="Entity does not match this facility.")

    repayment = FacilityRepayment(
        facility_id=facility_id, **payload.model_dump(), created_by_user_id=user.id,
    )
    db.add(repayment)
    await db.flush()
    await record_audit_event(
        db, module=MODULE.value, action="REPAYMENT_CREATE", record_type="FacilityRepayment",
        record_id=str(repayment.id), user_id=user.id, legal_entity_id=repayment.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(repayment)
    return repayment


@router.get("/facilities/{facility_id}/repayments", response_model=list[RepaymentOut])
async def list_repayments(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    result = await db.execute(
        select(FacilityRepayment).where(FacilityRepayment.facility_id == facility_id)
        .order_by(FacilityRepayment.due_date)
    )
    return list(result.scalars().all())


@router.get("/repayments/{repayment_id}", response_model=RepaymentOut)
async def get_repayment(
    repayment_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityRepayment:
    repayment = await db.get(FacilityRepayment, repayment_id)
    if repayment is None:
        raise HTTPException(status_code=404, detail="Repayment not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, repayment.legal_entity_id)
    return repayment


@router.post("/repayments/{repayment_id}/pay", response_model=RepaymentOut)
async def pay_repayment(
    repayment_id: uuid.UUID, payload: RepaymentPaymentRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityRepayment:
    """
    SECTION 2/3/4: locks the repayment (idempotency - a concurrent second
    payment call serializes and then sees PAID/updated paid_amount) and
    the facility (so the current_drawn_amount and any linked sub-limit
    decrement happen against locked, current state). Overpayment beyond
    the outstanding balance is rejected in record_repayment_payment.
    """
    repayment = await load_repayment_for_update(db, repayment_id)
    if repayment is None:
        raise HTTPException(status_code=404, detail="Repayment not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.EXECUTE, repayment.legal_entity_id)
    if repayment.status in (RepaymentStatus.PAID, RepaymentStatus.CANCELLED):
        raise HTTPException(status_code=400, detail=f"Cannot pay a repayment in status {repayment.status.value}.")

    facility = await load_facility_for_update(db, repayment.facility_id)
    if facility is None:
        raise HTTPException(status_code=404, detail="Facility not found for this repayment.")
    await record_repayment_payment(
        db, facility, repayment, payload.paid_amount, payload.actual_payment_date, user.id,
    )
    await record_audit_event(
        db, module=MODULE.value, action="REPAYMENT_PAID", record_type="FacilityRepayment",
        record_id=str(repayment.id), user_id=user.id, legal_entity_id=repayment.legal_entity_id,
        new_value={"paid_amount": str(payload.paid_amount)},
    )
    await db.commit()
    await db.refresh(repayment)
    return repayment


@router.post(
    "/facilities/{facility_id}/fees", response_model=FeeOut, status_code=status.HTTP_201_CREATED
)
async def create_fee(
    facility_id: uuid.UUID, payload: FeeCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityFee:
    facility = await _load_facility(db, facility_id)
    await assert_entity_access(db, user, MODULE, TreasuryAction.CREATE, payload.legal_entity_id)
    if facility.legal_entity_id != payload.legal_entity_id:
        raise HTTPException(status_code=400, detail="Entity does not match this facility.")

    fee = FacilityFee(facility_id=facility_id, **payload.model_dump())
    db.add(fee)
    await db.flush()
    db.add(FacilityEvent(
        facility_id=facility_id, event_type=FacilityEventType.FEE_CHARGED,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"{fee.fee_type.value.title()} fee of {fee.amount} {fee.currency_code} charged.",
        related_record_type="FacilityFee", related_record_id=str(fee.id),
    ))
    await record_audit_event(
        db, module=MODULE.value, action="FEE_CREATE", record_type="FacilityFee",
        record_id=str(fee.id), user_id=user.id, legal_entity_id=fee.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(fee)
    return fee


@router.get("/facilities/{facility_id}/fees", response_model=list[FeeOut])
async def list_fees(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    result = await db.execute(
        select(FacilityFee).where(FacilityFee.facility_id == facility_id).order_by(FacilityFee.due_date)
    )
    return list(result.scalars().all())


@router.post(
    "/facilities/{facility_id}/covenants", response_model=CovenantOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_covenant(
    facility_id: uuid.UUID, payload: CovenantCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityCovenant:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.CONFIGURE)

    covenant = FacilityCovenant(
        facility_id=facility_id, **payload.model_dump(), status=CovenantStatus.DATA_REQUIRED,
    )
    db.add(covenant)
    await db.flush()
    await record_audit_event(
        db, module=MODULE.value, action="COVENANT_CREATE", record_type="FacilityCovenant",
        record_id=str(covenant.id), user_id=user.id, legal_entity_id=facility.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(covenant)
    return covenant


@router.get("/facilities/{facility_id}/covenants", response_model=list[CovenantOut])
async def list_covenants(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    result = await db.execute(
        select(FacilityCovenant).where(FacilityCovenant.facility_id == facility_id)
    )
    return list(result.scalars().all())


@router.patch("/covenants/{covenant_id}", response_model=CovenantOut)
async def update_covenant(
    covenant_id: uuid.UUID, payload: CovenantUpdate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FacilityCovenant:
    covenant = await db.get(FacilityCovenant, covenant_id)
    if covenant is None:
        raise HTTPException(status_code=404, detail="Covenant not found")
    facility = await db.get(Facility, covenant.facility_id)
    if facility is None:
        raise HTTPException(status_code=404, detail="Facility not found for this covenant.")
    await _assert_facility_scope(db, user, facility, TreasuryAction.CONFIGURE)

    previous_status = covenant.status
    if payload.current_value is not None:
        covenant.current_value = payload.current_value
    if payload.measurement_date is not None:
        covenant.measurement_date = payload.measurement_date

    if covenant.current_value is None or covenant.threshold is None:
        covenant.status = CovenantStatus.DATA_REQUIRED
    else:
        compliant = evaluate_covenant(covenant.operator.value, covenant.current_value, covenant.threshold)
        covenant.headroom = covenant.current_value - covenant.threshold
        if compliant:
            if covenant.warning_threshold is not None and not evaluate_covenant(
                covenant.operator.value, covenant.current_value, covenant.warning_threshold
            ):
                covenant.status = CovenantStatus.WARNING
            else:
                covenant.status = CovenantStatus.COMPLIANT
        else:
            covenant.status = CovenantStatus.BREACH

    if covenant.status != previous_status and covenant.status in (
        CovenantStatus.WARNING, CovenantStatus.BREACH
    ):
        event_type = (
            FacilityEventType.COVENANT_BREACH if covenant.status == CovenantStatus.BREACH
            else FacilityEventType.COVENANT_WARNING
        )
        db.add(FacilityEvent(
            facility_id=facility.id, event_type=event_type,
            event_date=datetime.datetime.now(datetime.UTC),
            description=f"Covenant '{covenant.name}' status changed to {covenant.status.value}.",
            related_record_type="FacilityCovenant", related_record_id=str(covenant.id),
            previous_value={"status": previous_status.value}, new_value={"status": covenant.status.value},
        ))

    await record_audit_event(
        db, module=MODULE.value, action="COVENANT_UPDATE", record_type="FacilityCovenant",
        record_id=str(covenant.id), user_id=user.id, legal_entity_id=facility.legal_entity_id,
        previous_value={"status": previous_status.value}, new_value={"status": covenant.status.value},
    )
    await db.commit()
    await db.refresh(covenant)
    return covenant


@router.post(
    "/facilities/{facility_id}/collateral", response_model=CollateralOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_collateral(
    facility_id: uuid.UUID, payload: CollateralCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CollateralOut:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.CREATE)

    collateral = FacilityCollateral(facility_id=facility_id, **payload.model_dump())
    db.add(collateral)
    await db.flush()
    db.add(FacilityEvent(
        facility_id=facility_id, event_type=FacilityEventType.COLLATERAL_ADDED,
        event_date=datetime.datetime.now(datetime.UTC),
        description=f"{collateral.collateral_type.value.title()} collateral of "
                    f"{collateral.value} {collateral.currency_code} added.",
        related_record_type="FacilityCollateral", related_record_id=str(collateral.id),
    ))
    await record_audit_event(
        db, module=MODULE.value, action="COLLATERAL_CREATE", record_type="FacilityCollateral",
        record_id=str(collateral.id), user_id=user.id, legal_entity_id=facility.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(collateral)
    return CollateralOut(
        id=collateral.id, facility_id=collateral.facility_id, collateral_type=collateral.collateral_type,
        value=collateral.value, currency_code=collateral.currency_code, haircut_pct=collateral.haircut_pct,
        eligible_value=collateral.eligible_value, status=collateral.status,
    )


@router.get("/facilities/{facility_id}/collateral", response_model=list[CollateralOut])
async def list_collateral(
    facility_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    facility = await _load_facility(db, facility_id)
    await _assert_facility_scope(db, user, facility, TreasuryAction.VIEW)
    result = await db.execute(
        select(FacilityCollateral).where(FacilityCollateral.facility_id == facility_id)
    )
    items = list(result.scalars().all())
    return [
        CollateralOut(
            id=c.id, facility_id=c.facility_id, collateral_type=c.collateral_type, value=c.value,
            currency_code=c.currency_code, haircut_pct=c.haircut_pct, eligible_value=c.eligible_value,
            status=c.status,
        )
        for c in items
    ]
