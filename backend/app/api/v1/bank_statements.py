"""
Bank statement evidence viewer (Stage 5A).

Read-only: `BankStatementTransaction` rows are only ever created via the
Excel Data Hub import pipeline (app/services/bank_statement_excel_template.py).
This router exists purely so an authorized user can inspect imported
evidence (SECTION 24: "Statement transaction view") - it never creates,
modifies, or matches anything. Matching against TreasuryTransaction is
explicitly Stage 5C's job, not this router's.
"""
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
from app.models.bank_statement import BankStatementTransaction
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.bank_statement import BankStatementTransactionOut

router = APIRouter(prefix="/bank-statements", tags=["bank-statements"])

MODULE = TreasuryModule.BANK_RECONCILIATION


@router.get("/transactions", response_model=list[BankStatementTransactionOut])
async def list_bank_statement_transactions(
    legal_entity_id: uuid.UUID | None = None,
    bank_account_id: uuid.UUID | None = None,
    import_batch_id: uuid.UUID | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list:
    """
    SECTION 9/24: entity-scoped exactly like every other list endpoint in
    this codebase - an Entity A user can never enumerate Entity B's
    statement evidence, by filter or otherwise, since the scope is
    resolved server-side from the caller's own authorized entities, never
    from a client-supplied filter alone.
    """
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No BANK_RECONCILIATION:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(BankStatementTransaction)
    stmt = apply_resolved_entity_scope(stmt, BankStatementTransaction.legal_entity_id, resolved_ids)
    if legal_entity_id:
        stmt = stmt.where(BankStatementTransaction.legal_entity_id == legal_entity_id)
    if bank_account_id:
        stmt = stmt.where(BankStatementTransaction.bank_account_id == bank_account_id)
    if import_batch_id:
        stmt = stmt.where(BankStatementTransaction.import_batch_id == import_batch_id)
    stmt = stmt.order_by(BankStatementTransaction.transaction_date.desc()).limit(min(limit, 500))
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/transactions/{transaction_id}", response_model=BankStatementTransactionOut)
async def get_bank_statement_transaction(
    transaction_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankStatementTransaction:
    """
    Detail-level authorization checked against the RECORD's own entity,
    never a request parameter - the same discipline every other
    single-record endpoint in this codebase already follows (a
    transaction_id cannot be enumerated across entities merely because
    the caller holds VIEW for *some* entity).
    """
    transaction = await db.get(BankStatementTransaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Bank statement transaction not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, transaction.legal_entity_id)
    return transaction
