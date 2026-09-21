import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.audit import AuditEvent
from app.models.rbac import TreasuryAction, TreasuryModule, User

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEventOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    action: str
    module: str
    record_type: str
    record_id: str
    legal_entity_id: uuid.UUID | None
    reason: str | None

    model_config = {"from_attributes": True}


@router.get("", response_model=list[AuditEventOut])
async def list_audit_events(
    module: str | None = None,
    record_type: str | None = None,
    record_id: str | None = None,
    legal_entity_id: uuid.UUID | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AuditEvent]:
    """
    Server-side entity scoping (Stage 2 Hardening): audit events carry
    the legal_entity_id of the record they describe, and are just as
    sensitive as the underlying data (an audit trail can reveal amounts,
    counterparties, and business activity even without the source
    record) - so they're scoped exactly like any other list endpoint,
    never returned unfiltered to anyone with bare AUDIT:VIEW.
    """
    scope = await get_authorized_scope(db, user, TreasuryModule.AUDIT, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No AUDIT:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(AuditEvent)
    if resolved_ids != "ALL":
        from sqlalchemy import or_

        # Entity-tagged events are scoped normally; events with no entity
        # (e.g. currency/account-type admin actions) are visible to any
        # authorized viewer - they're global config, not entity data.
        stmt = stmt.where(
            or_(AuditEvent.legal_entity_id.is_(None), AuditEvent.legal_entity_id.in_(resolved_ids))
        ) if resolved_ids else stmt.where(AuditEvent.legal_entity_id.is_(None))
    if module:
        stmt = stmt.where(AuditEvent.module == module)
    if record_type:
        stmt = stmt.where(AuditEvent.record_type == record_type)
    if record_id:
        stmt = stmt.where(AuditEvent.record_id == record_id)
    if legal_entity_id:
        stmt = stmt.where(AuditEvent.legal_entity_id == legal_entity_id)
    stmt = stmt.order_by(AuditEvent.created_at.desc()).limit(min(limit, 500))
    result = await db.execute(stmt)
    return list(result.scalars().all())
