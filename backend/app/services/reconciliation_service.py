"""
Reconciliation run + configuration service (Stage 5B).

Implements ONLY the run lifecycle and execution BOUNDARY described in
the Stage 5B run-creation/execution-boundary addendum - never the
matching algorithm itself (Stage 5C). `execute_reconciliation_run`
below deliberately does nothing but count in-scope
BankStatementTransaction rows and flip the run's own status; it never
reads TreasuryTransaction for scoring purposes, never creates a
ReconciliationMatchSuggestion or ReconciliationOpenItem row, and never
touches any cash-affecting model.
"""
import datetime
import uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import BankStatementTransaction
from app.models.reconciliation import (
    RECONCILIATION_RUN_STATUS_TRANSITIONS,
    ReconciliationConfiguration,
    ReconciliationRun,
    ReconciliationRunStatus,
)


async def load_run_for_update(db: AsyncSession, run_id: uuid.UUID) -> ReconciliationRun | None:
    """
    SECTION 8/24 (run-boundary addendum): row-locks the run before any
    lifecycle transition, the same discipline already established for
    every other stateful workflow in this codebase
    (`load_facility_for_update`, `load_investment_for_update`,
    `load_import_batch_for_update`). A second, concurrent request against
    the same run serializes here rather than racing.
    """
    result = await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id).with_for_update())
    return result.scalar_one_or_none()


async def resolve_effective_configuration(
    db: AsyncSession, legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID, currency_code: str,
) -> ReconciliationConfiguration | None:
    """
    SECTION 3.2 (Stage 5B spec): "if configuration is not explicitly
    supplied, the server must resolve the applicable effective
    configuration." Resolution order, most specific first, among rows
    with `is_current=True` and `is_active=True`:

        1. exact (entity, bank_account, currency)
        2. (entity, bank_account), currency-agnostic
        3. (entity, currency), account-agnostic
        4. entity-wide default (all else null)
        5. global/system-wide default (everything null)

    Returns None if nothing matches at any level - a run may legitimately
    have no configuration yet (Stage 5C will then need one before it can
    do anything, but Stage 5B's own execution boundary does not require
    one to exist).
    """
    candidates: list[dict[str, uuid.UUID | str | None]] = [
        {"legal_entity_id": legal_entity_id, "bank_account_id": bank_account_id, "currency_code": currency_code},
        {"legal_entity_id": legal_entity_id, "bank_account_id": bank_account_id, "currency_code": None},
        {"legal_entity_id": legal_entity_id, "bank_account_id": None, "currency_code": currency_code},
        {"legal_entity_id": legal_entity_id, "bank_account_id": None, "currency_code": None},
        {"legal_entity_id": None, "bank_account_id": None, "currency_code": None},
    ]
    for filters in candidates:
        stmt = select(ReconciliationConfiguration).where(
            ReconciliationConfiguration.is_current.is_(True),
            ReconciliationConfiguration.is_active.is_(True),
        )
        for column_name, value in filters.items():
            column = getattr(ReconciliationConfiguration, column_name)
            stmt = stmt.where(column.is_(None) if value is None else column == value)
        result = await db.execute(stmt)
        config = result.scalars().first()
        if config is not None:
            return config
    return None


async def create_configuration_version(
    db: AsyncSession, payload: dict, user_id,
) -> ReconciliationConfiguration:
    """
    SECTION 13/14: a configuration change is always a NEW row (mirrors
    FXRate's own versioning discipline) - the prior current row for the
    same (entity, bank_account, currency) identity is superseded, never
    edited in place or deleted, so every ReconciliationRun's own
    `configuration_id` reference remains permanently reproducible.
    """
    identity_stmt = select(ReconciliationConfiguration).where(
        ReconciliationConfiguration.is_current.is_(True),
        ReconciliationConfiguration.legal_entity_id == payload.get("legal_entity_id"),
        ReconciliationConfiguration.bank_account_id == payload.get("bank_account_id"),
        ReconciliationConfiguration.currency_code == payload.get("currency_code"),
    )
    existing = (await db.execute(identity_stmt)).scalars().first()

    next_version = 1
    if existing is not None:
        next_version = existing.version + 1

    new_config = ReconciliationConfiguration(
        legal_entity_id=payload.get("legal_entity_id"), bank_account_id=payload.get("bank_account_id"),
        currency_code=payload.get("currency_code"),
        amount_tolerance_pct=payload.get("amount_tolerance_pct", Decimal(0)),
        date_tolerance_days=payload.get("date_tolerance_days", 0),
        high_value_threshold=payload.get("high_value_threshold"),
        duplicate_policy=payload.get("duplicate_policy"),
        matching_rule_config=payload.get("matching_rule_config") or {},
        is_active=True, version=next_version, is_current=True,
        effective_from=payload.get("effective_from") or datetime.date.today(),
        created_by_user_id=user_id,
    )
    db.add(new_config)
    await db.flush()

    if existing is not None:
        existing.is_current = False
        existing.superseded_by_id = new_config.id
        await db.flush()

    return new_config


async def transition_run_status(
    db: AsyncSession, run: ReconciliationRun, new_status: ReconciliationRunStatus,
) -> ReconciliationRun:
    allowed = RECONCILIATION_RUN_STATUS_TRANSITIONS.get(run.status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition reconciliation run from {run.status.value} to "
                   f"{new_status.value}. Allowed: {[s.value for s in allowed] or 'none'}.",
        )
    run.status = new_status
    await db.flush()
    return run


async def execute_reconciliation_run(db: AsyncSession, run: ReconciliationRun, user_id) -> ReconciliationRun:
    """
    SECTION 4-10 of the run-boundary addendum: the execution BOUNDARY
    only. Transitions READY -> RUNNING -> COMPLETED (or FAILED on an
    unexpected error), and populates result-count infrastructure by
    counting BankStatementTransaction rows already within the run's own
    authorized scope (entity + bank account + period) - the exact same
    scope the run itself was created against, never broadened.

    This function creates ZERO ReconciliationMatchSuggestion rows and
    ZERO ReconciliationOpenItem rows - there is no matching engine here
    yet (Stage 5C). `eligible_transaction_count` is set equal to
    `statement_transaction_count` as an explicit Stage 5B placeholder;
    Stage 5C will define real eligibility rules (e.g. excluding rows
    already consumed by an earlier run) and refine this.
    """
    await transition_run_status(db, run, ReconciliationRunStatus.RUNNING)
    run.started_at = datetime.datetime.now(datetime.UTC)
    run.executed_by_user_id = user_id
    await db.flush()

    try:
        count_stmt = select(func.count(BankStatementTransaction.id)).where(
            BankStatementTransaction.legal_entity_id == run.legal_entity_id,
            BankStatementTransaction.bank_account_id == run.bank_account_id,
            BankStatementTransaction.transaction_date >= run.period_start,
            BankStatementTransaction.transaction_date <= run.period_end,
        )
        count = (await db.execute(count_stmt)).scalar_one()
        run.statement_transaction_count = count
        run.eligible_transaction_count = count

        await transition_run_status(db, run, ReconciliationRunStatus.COMPLETED)
        run.completed_at = datetime.datetime.now(datetime.UTC)
    except Exception as exc:
        run.status = ReconciliationRunStatus.FAILED
        run.failure_reason = str(exc)
        run.completed_at = datetime.datetime.now(datetime.UTC)
        await db.flush()
        raise

    await db.flush()
    return run
