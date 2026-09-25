"""
Reconciliation run + configuration API (Stage 5B).

SECTION 19 of the Stage 5B spec is explicit: do not create an endpoint
that pretends matching exists (e.g. no `/auto-match`). Every endpoint
here manages the DATA MODEL and the run LIFECYCLE only.
"""
import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException
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
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.models.reconciliation import (
    ReconciliationConfiguration,
    ReconciliationMatchSuggestion,
    ReconciliationOpenItem,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.schemas.reconciliation import (
    ReconciliationConfigurationCreate,
    ReconciliationConfigurationOut,
    ReconciliationMatchSuggestionOut,
    ReconciliationOpenItemOut,
    ReconciliationRunCreate,
    ReconciliationRunOut,
    ReconciliationRunUpdate,
)
from app.services.audit_service import record_audit_event
from app.services.reconciliation_service import (
    create_configuration_version,
    execute_reconciliation_run,
    load_run_for_update,
    resolve_effective_configuration,
    transition_run_status,
    validate_configuration_scope,
)

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

MODULE = TreasuryModule.BANK_RECONCILIATION


@router.post("/runs", response_model=ReconciliationRunOut, status_code=201)
async def create_reconciliation_run(
    payload: ReconciliationRunCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReconciliationRun:
    """
    SECTION 2/3 (run-boundary addendum): creation validates and resolves
    scope/configuration eagerly and NEVER matches anything - zero
    ReconciliationMatchSuggestion/ReconciliationOpenItem rows are created
    here, regardless of how much bank statement evidence already exists
    in scope.
    """
    await assert_entity_access(db, user, MODULE, TreasuryAction.CREATE, payload.legal_entity_id)

    if payload.period_start > payload.period_end:
        raise HTTPException(status_code=400, detail="period_start must not be after period_end.")

    account = await db.get(BankAccount, payload.bank_account_id)
    if account is None:
        raise HTTPException(status_code=400, detail="Bank account not found.")
    if account.legal_entity_id != payload.legal_entity_id:
        raise HTTPException(
            status_code=400, detail="Bank account does not belong to the specified entity.",
        )

    configuration = None
    if payload.configuration_id is not None:
        configuration = await db.get(ReconciliationConfiguration, payload.configuration_id)
        if configuration is None:
            raise HTTPException(status_code=400, detail="Reconciliation configuration not found.")
        if not configuration.is_current or not configuration.is_active:
            raise HTTPException(
                status_code=400,
                detail="The selected reconciliation configuration is not currently active.",
            )
        # SECTION "ISSUE 1" (Stage 5B configuration-integrity hardening
        # patch): existence + is_current/is_active alone is insufficient
        # - a scope check is required so an Entity A run cannot be
        # created against an Entity B (or Account B, or a mismatched
        # currency) configuration merely because that configuration
        # happens to exist and be active.
        validate_configuration_scope(configuration, payload.legal_entity_id, payload.bank_account_id, account.currency_code)
    else:
        configuration = await resolve_effective_configuration(
            db, payload.legal_entity_id, payload.bank_account_id, account.currency_code,
        )

    run = ReconciliationRun(
        legal_entity_id=payload.legal_entity_id, bank_account_id=payload.bank_account_id,
        period_start=payload.period_start, period_end=payload.period_end,
        configuration_id=configuration.id if configuration else None,
        status=ReconciliationRunStatus.READY, notes=payload.notes, created_by_user_id=user.id,
    )
    db.add(run)
    await db.flush()
    await record_audit_event(
        db, module=MODULE.value, action="CREATE", record_type="ReconciliationRun",
        record_id=str(run.id), user_id=user.id, legal_entity_id=run.legal_entity_id,
        new_value={"bank_account_id": str(run.bank_account_id), "period_start": str(run.period_start),
                   "period_end": str(run.period_end), "configuration_id": str(configuration.id) if configuration else None},
    )
    await db.commit()
    await db.refresh(run)
    return run


@router.get("/runs", response_model=list[ReconciliationRunOut])
async def list_reconciliation_runs(
    legal_entity_id: uuid.UUID | None = None,
    bank_account_id: uuid.UUID | None = None,
    status_filter: ReconciliationRunStatus | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No BANK_RECONCILIATION:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(ReconciliationRun)
    stmt = apply_resolved_entity_scope(stmt, ReconciliationRun.legal_entity_id, resolved_ids)
    if legal_entity_id:
        stmt = stmt.where(ReconciliationRun.legal_entity_id == legal_entity_id)
    if bank_account_id:
        stmt = stmt.where(ReconciliationRun.bank_account_id == bank_account_id)
    if status_filter:
        stmt = stmt.where(ReconciliationRun.status == status_filter)
    stmt = stmt.order_by(ReconciliationRun.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _get_run_or_404(db: AsyncSession, run_id: uuid.UUID) -> ReconciliationRun:
    run = await db.get(ReconciliationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")
    return run


@router.get("/runs/{run_id}", response_model=ReconciliationRunOut)
async def get_reconciliation_run(
    run_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> ReconciliationRun:
    run = await _get_run_or_404(db, run_id)
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, run.legal_entity_id)
    return run


@router.patch("/runs/{run_id}", response_model=ReconciliationRunOut)
async def update_reconciliation_run(
    run_id: uuid.UUID, payload: ReconciliationRunUpdate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReconciliationRun:
    run = await load_run_for_update(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.EDIT, run.legal_entity_id)
    if run.status not in (ReconciliationRunStatus.DRAFT, ReconciliationRunStatus.READY):
        raise HTTPException(status_code=400, detail="Only a DRAFT or READY run's notes can be edited.")
    if payload.notes is not None:
        run.notes = payload.notes
    await db.commit()
    await db.refresh(run)
    return run


@router.post("/runs/{run_id}/execute", response_model=ReconciliationRunOut)
async def execute_run_endpoint(
    run_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> ReconciliationRun:
    """
    SECTION 6/7/8 (run-boundary addendum): row-locked, revalidated, and
    concurrency-safe - a second concurrent execution request for the
    same run blocks on the lock, then observes the already-advanced
    status and is rejected (a run can only ever be picked up by exactly
    one execution). Implements the execution BOUNDARY only - see
    execute_reconciliation_run's own docstring for what it deliberately
    does not do.
    """
    run = await load_run_for_update(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.EXECUTE, run.legal_entity_id)

    if run.status != ReconciliationRunStatus.READY:
        raise HTTPException(
            status_code=400,
            detail=f"Only a READY run can be executed (current status: {run.status.value}).",
        )

    # SECTION 7: revalidate critical conditions at execution time, not
    # merely at creation time - the bank account may have been
    # reassigned or the configuration may have been superseded since.
    account = await db.get(BankAccount, run.bank_account_id)
    if account is None or account.legal_entity_id != run.legal_entity_id:
        run.status = ReconciliationRunStatus.FAILED
        run.failure_reason = "Bank account no longer belongs to the run's entity."
        run.completed_at = datetime.datetime.now(datetime.UTC)
        await db.commit()
        await db.refresh(run)
        return run
    if run.configuration_id is not None:
        # SECTION "ISSUE 2" (Stage 5B configuration-integrity hardening
        # patch): a run is permanently tied to the EXACT configuration
        # version stored on it at creation - `run.configuration_id`
        # never changes and execution never re-resolves a newer version.
        # A later configuration change only ever sets `is_current=False`
        # on the superseded row (create_configuration_version never
        # touches `is_active`); even if a future mechanism DOES
        # deactivate a historical version, execution must still only
        # fail when the configuration cannot be loaded at all - never
        # merely because it is no longer current/active. Both flags are
        # deliberately NOT checked here.
        configuration = await db.get(ReconciliationConfiguration, run.configuration_id)
        if configuration is None:
            run.status = ReconciliationRunStatus.FAILED
            run.failure_reason = "The run's reconciliation configuration no longer exists."
            run.completed_at = datetime.datetime.now(datetime.UTC)
            await db.commit()
            await db.refresh(run)
            return run

    await record_audit_event(
        db, module=MODULE.value, action="EXECUTE_START", record_type="ReconciliationRun",
        record_id=str(run.id), user_id=user.id, legal_entity_id=run.legal_entity_id,
    )

    try:
        run = await execute_reconciliation_run(db, run, user.id)
    except Exception:  # noqa: BLE001 - execution failures land in FAILED, never crash the request
        await db.commit()
        await db.refresh(run)
        await record_audit_event(
            db, module=MODULE.value, action="EXECUTE_FAILED", record_type="ReconciliationRun",
            record_id=str(run.id), user_id=user.id, legal_entity_id=run.legal_entity_id,
            reason=run.failure_reason,
        )
        return run

    await record_audit_event(
        db, module=MODULE.value, action="EXECUTE_COMPLETE", record_type="ReconciliationRun",
        record_id=str(run.id), user_id=user.id, legal_entity_id=run.legal_entity_id,
        new_value={"statement_transaction_count": run.statement_transaction_count,
                   "eligible_transaction_count": run.eligible_transaction_count},
    )
    await db.commit()
    await db.refresh(run)
    return run


@router.post("/runs/{run_id}/cancel", response_model=ReconciliationRunOut)
async def cancel_run_endpoint(
    run_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> ReconciliationRun:
    run = await load_run_for_update(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.CLOSE, run.legal_entity_id)

    run = await transition_run_status(db, run, ReconciliationRunStatus.CANCELLED)
    await record_audit_event(
        db, module=MODULE.value, action="CANCEL", record_type="ReconciliationRun",
        record_id=str(run.id), user_id=user.id, legal_entity_id=run.legal_entity_id,
    )
    await db.commit()
    await db.refresh(run)
    return run


@router.get("/runs/{run_id}/suggestions", response_model=list[ReconciliationMatchSuggestionOut])
async def list_run_suggestions(
    run_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> list:
    """
    SECTION 19: this endpoint exists so Stage 5C has somewhere to write
    to and Stage 5D/5H have somewhere to read from - it will always
    return an empty list in Stage 5B, since nothing in this stage ever
    creates a ReconciliationMatchSuggestion row.
    """
    run = await _get_run_or_404(db, run_id)
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, run.legal_entity_id)
    result = await db.execute(
        select(ReconciliationMatchSuggestion).where(ReconciliationMatchSuggestion.reconciliation_run_id == run_id)
    )
    return list(result.scalars().all())


@router.get("/runs/{run_id}/open-items", response_model=list[ReconciliationOpenItemOut])
async def list_run_open_items(
    run_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> list:
    """Same note as list_run_suggestions - always empty in Stage 5B."""
    run = await _get_run_or_404(db, run_id)
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, run.legal_entity_id)
    result = await db.execute(
        select(ReconciliationOpenItem).where(ReconciliationOpenItem.reconciliation_run_id == run_id)
    )
    return list(result.scalars().all())


@router.get("/configurations", response_model=list[ReconciliationConfigurationOut])
async def list_reconciliation_configurations(
    legal_entity_id: uuid.UUID | None = None,
    include_superseded: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No BANK_RECONCILIATION:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    stmt = select(ReconciliationConfiguration)
    if not include_superseded:
        stmt = stmt.where(ReconciliationConfiguration.is_current.is_(True))
    if legal_entity_id:
        stmt = stmt.where(ReconciliationConfiguration.legal_entity_id == legal_entity_id)
    elif not scope.unrestricted:
        # A null legal_entity_id means "applies broadly" (entity-wide/
        # system-wide default) - such rows must remain visible to a
        # scoped user too, so apply_resolved_entity_scope's plain
        # entity_id.in_(...) (which excludes NULL) cannot be reused
        # as-is here.
        resolved_ids = await resolve_scope_entity_ids(db, scope)
        if resolved_ids != "ALL":
            stmt = stmt.where(
                ReconciliationConfiguration.legal_entity_id.is_(None)
                | ReconciliationConfiguration.legal_entity_id.in_(resolved_ids)
            )
    result = await db.execute(stmt.order_by(ReconciliationConfiguration.created_at.desc()))
    return list(result.scalars().all())


@router.post("/configurations", response_model=ReconciliationConfigurationOut, status_code=201)
async def create_reconciliation_configuration(
    payload: ReconciliationConfigurationCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReconciliationConfiguration:
    if payload.legal_entity_id is not None:
        await assert_entity_access(db, user, MODULE, TreasuryAction.CONFIGURE, payload.legal_entity_id)
    else:
        scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.CONFIGURE)
        if not scope.unrestricted:
            raise HTTPException(
                status_code=403,
                detail="Only a group-wide/unrestricted user may create a system-wide default configuration.",
            )

    if payload.bank_account_id is not None:
        account = await db.get(BankAccount, payload.bank_account_id)
        if account is None:
            raise HTTPException(status_code=400, detail="Bank account not found.")
        if payload.legal_entity_id is not None and account.legal_entity_id != payload.legal_entity_id:
            raise HTTPException(status_code=400, detail="Bank account does not belong to the specified entity.")

    config = await create_configuration_version(db, payload.model_dump(), user.id)
    await record_audit_event(
        db, module=MODULE.value, action="CONFIGURE", record_type="ReconciliationConfiguration",
        record_id=str(config.id), user_id=user.id, legal_entity_id=payload.legal_entity_id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(config)
    return config
