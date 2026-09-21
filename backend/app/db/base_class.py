"""
Shared declarative base and reusable mixins.

These mixins encode cross-cutting architectural principles so every
domain model inherits them consistently instead of re-implementing
ad hoc conventions:

- PRINCIPLE 4: audit trail on material financial change
- PRINCIPLE 5: historical data is never silently overwritten (soft delete,
  not hard delete, for anything with financial/audit relevance)
- PRINCIPLE 6: DB is source of truth -> server-side defaults where possible
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Project-wide declarative base."""


class UUIDPKMixin:
    """Primary key as a UUID, generated application-side (portable across DBs)."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """created_at / updated_at, DB-managed so app code can't forget to set them."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """
    Soft deletion only. Rows are never physically removed for entities that
    carry financial or audit significance (PRINCIPLE 5).
    """

    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def deactivate(self) -> None:
        self.is_active = False
        self.deactivated_at = datetime.now(UTC)
