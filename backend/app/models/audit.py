"""
Audit & Activity History foundation.

PRINCIPLE 4: every material financial change has an audit trail.
This is an append-only table: rows are never updated or deleted by
application code. Write through app.services.audit_service only.
"""
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin, UUIDPKMixin


class AuditEvent(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "audit_events"

    # Who
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    # What
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    module: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # Entity / Record context
    group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    record_type: Mapped[str] = mapped_column(String(100), nullable=False)
    record_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # Change detail
    previous_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    approval_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Source
    source: Mapped[str] = mapped_column(String(50), default="API", nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
