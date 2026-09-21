"""
Central audit write path.

PRINCIPLE 4: every material financial change has an audit trail.
All application code that mutates a financial/administrative record
should call `record_audit_event` in the same transaction as the change,
rather than writing to AuditEvent directly. This keeps the audit shape
consistent and makes it easy to later add e.g. async fan-out.
"""
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent


async def record_audit_event(
    db: AsyncSession,
    *,
    module: str,
    action: str,
    record_type: str,
    record_id: str,
    user_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    previous_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    reason: str | None = None,
    approval_reference: str | None = None,
    source: str = "API",
    ip_address: str | None = None,
    session_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        user_id=user_id,
        action=action,
        module=module,
        group_id=group_id,
        legal_entity_id=legal_entity_id,
        record_type=record_type,
        record_id=record_id,
        previous_value=previous_value,
        new_value=new_value,
        reason=reason,
        approval_reference=approval_reference,
        source=source,
        ip_address=ip_address,
        session_id=session_id,
    )
    db.add(event)
    await db.flush()
    return event
