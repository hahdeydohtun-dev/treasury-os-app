"""
Treasury Transaction / Cash Event — the common financial data layer
(SECTION 4/5/6 of the Stage 2 spec).

This is the single coherent model that every future module (bank
transactions, payments, collections, loans, investments, intercompany,
etc.) feeds through `event_type_code`, instead of each module inventing
its own incompatible transaction table. It is deliberately generic:
module-specific detail belongs in specialized tables (e.g. a future Loan
model) that reference a TreasuryTransaction for its cash-flow leg, not in
this table itself.

Every relevant financial object carries the full multi-currency shape
(transaction/functional/reporting amount + currency, plus the FX rate that
relates them) per PRINCIPLE 2 and the currency architecture already
established for FXRate.
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin, UUIDPKMixin
from app.models.lookup import CashDirection


class TransactionStatus(str, Enum):
    PENDING = "PENDING"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"


class TreasuryTransaction(Base, UUIDPKMixin, TimestampMixin):
    """
    A single treasury cash event. Never physically deleted once POSTED —
    correcting a posted transaction is a CANCELLED status change plus a new
    offsetting/corrected transaction, not an in-place edit, to preserve
    PRINCIPLE 5 (historical financial data is never silently overwritten).
    """
    __tablename__ = "treasury_transactions"

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business_units.id"), nullable=True
    )

    event_type_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("cash_event_types.code"), nullable=False, index=True
    )
    direction: Mapped[CashDirection] = mapped_column(nullable=False, index=True)

    event_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    value_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    posting_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    # --- Multi-currency shape (mirrors FXRate's currency architecture) ---
    transaction_currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    transaction_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    functional_currency_code: Mapped[str | None] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=True
    )
    functional_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    reporting_currency_code: Mapped[str | None] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=True
    )
    reporting_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    exchange_rate_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    exchange_rate_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    exchange_rate_source: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # --- Bank linkage (nullable: not every cash event is bank-specific) ---
    bank_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("banks.id"), nullable=True
    )
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=True, index=True
    )

    # --- Transfer pairing (SECTION 6): both legs of a transfer share this ---
    transfer_pair_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    counterparty: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    narration: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[TransactionStatus] = mapped_column(
        default=TransactionStatus.POSTED, nullable=False
    )

    # --- Source traceability (SECTION 19) ---
    source_type: Mapped[str] = mapped_column(String(50), default="MANUAL", nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_record_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True, index=True
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
