"""
Expected Collections / Expected Payments — forward-looking cash flow
inputs (SECTION 12 D/E). These feed the future 13-Week Cash Flow Forecast
Engine (SECTION 29) as the "Expected collections" / "Expected payments"
line items; they are intentionally separate from `TreasuryTransaction`
(which represents actual, posted cash events) since forecast inputs have
a different lifecycle (probability, status) and are not yet cash.
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin, UUIDPKMixin


class ForecastItemStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"
    OVERDUE = "OVERDUE"


class _ExpectedCashFlowMixin:
    """Shared shape for expected collections and expected payments."""

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    expected_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    counterparty: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    probability: Mapped[int | None] = mapped_column(nullable=True)  # 0-100
    status: Mapped[ForecastItemStatus] = mapped_column(
        default=ForecastItemStatus.OPEN, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Source traceability
    source_type: Mapped[str] = mapped_column(String(50), default="MANUAL", nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_record_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ExpectedCollection(Base, UUIDPKMixin, TimestampMixin, _ExpectedCashFlowMixin):
    __tablename__ = "expected_collections"

    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True, index=True
    )


class ExpectedPayment(Base, UUIDPKMixin, TimestampMixin, _ExpectedCashFlowMixin):
    __tablename__ = "expected_payments"

    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True, index=True
    )
