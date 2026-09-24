"""
Bank statement ingestion services specific to Stage 5A that live
ALONGSIDE (never inside) the generic Excel Data Hub engine
(app/services/excel_service.py) - deliberately additive, so the shared
engine that every other template also depends on is never modified for
a bank-statement-specific concern.

Covers:
  - statement-period overlap detection (SECTION 13) - informational,
    never a hard rejection, since a legitimate corrected/reissued
    statement can genuinely cover the same period twice
  - a row-locking wrapper for import-batch confirmation (SECTION 9 of
    the import-lifecycle addendum), because the generic engine's
    `confirm_import` does not itself acquire a row lock on the batch
    before transitioning it out of READY_FOR_IMPORT - a gap that
    predates Stage 5A but that Stage 5A's own concurrency requirement
    (two confirmations of the same batch must not double-import) makes
    necessary to close now. Fixing it here benefits every existing
    template's import confirmation too, not just BANK_STATEMENT.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import BankStatementTransaction
from app.models.excel_hub import ImportBatch, ImportIssue, IssueSeverity


async def load_import_batch_for_update(db: AsyncSession, batch_id: uuid.UUID) -> ImportBatch | None:
    """
    SECTION 9: acquires the same row lock discipline already established
    everywhere else in this codebase (Stage 3/4's `load_facility_for_update`/
    `load_investment_for_update`) for the import-batch confirmation
    transition. A second, concurrent confirmation request for the same
    batch blocks here until the first commits, then observes the
    already-advanced status and is rejected - it can never also import.
    """
    result = await db.execute(select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update())
    return result.scalar_one_or_none()


async def annotate_statement_period_overlap(db: AsyncSession, batch: ImportBatch) -> None:
    """
    SECTION 13: purely informational - never rejects the batch. For each
    distinct bank account referenced in this batch's rows, checks whether
    any PRIOR import (a different batch, already IMPORTED/
    PARTIALLY_IMPORTED) declared an overlapping statement period for that
    same account, and if so records a batch-level WARNING issue
    (row_number 0) naming the overlap - visible in the preview alongside
    every other validation result, satisfying "the second import should
    identify the overlap" without treating a legitimate reissued
    statement as an automatic duplicate.
    """
    if batch.template_code != "BANK_STATEMENT":
        return

    # Lazy import: avoids a circular import at module load time
    # (bank_statement_excel_template.py itself gets registered into
    # excel_templates.py's TEMPLATE_REGISTRY, which this module's sibling
    # API layer imports before this module's own top-level imports would
    # otherwise resolve).
    from app.services.bank_statement_excel_template import (
        _bank_account_by_number,
        _parse_statement_date,
    )

    seen_accounts: dict = {}
    for row in batch.raw_rows:
        account_number = row.get("Bank Account")
        period_start = _parse_statement_date(row.get("Statement Period Start"))
        period_end = _parse_statement_date(row.get("Statement Period End"))
        if not account_number or period_start is None or period_end is None:
            continue
        current = seen_accounts.get(account_number)
        if current is None:
            seen_accounts[account_number] = [period_start, period_end]
        else:
            current[0] = min(current[0], period_start)
            current[1] = max(current[1], period_end)

    for account_number, (period_start, period_end) in seen_accounts.items():
        bank_account = await _bank_account_by_number(db, account_number)
        if bank_account is None:
            continue

        overlap_stmt = (
            select(BankStatementTransaction.import_batch_id, BankStatementTransaction.statement_period_start,
                   BankStatementTransaction.statement_period_end)
            .where(
                BankStatementTransaction.bank_account_id == bank_account.id,
                BankStatementTransaction.import_batch_id != batch.id,
                BankStatementTransaction.statement_period_start <= period_end,
                BankStatementTransaction.statement_period_end >= period_start,
            )
            .distinct()
            .limit(5)
        )
        overlaps = (await db.execute(overlap_stmt)).all()
        if overlaps:
            overlap_batches = ", ".join(str(o.import_batch_id) for o in overlaps)
            db.add(ImportIssue(
                import_batch_id=batch.id, row_number=0, column_name="Statement Period",
                value=f"{period_start} to {period_end}", severity=IssueSeverity.WARNING,
                error_code="STATEMENT_PERIOD_OVERLAP",
                message=f"This statement's declared period for account {account_number} overlaps "
                        f"already-imported statement data from import batch(es): {overlap_batches}. "
                        f"This is not automatically rejected - a corrected/reissued statement can "
                        f"legitimately cover the same period. Individual overlapping transactions "
                        f"will still be caught by exact/potential duplicate detection.",
            ))
    await db.flush()
