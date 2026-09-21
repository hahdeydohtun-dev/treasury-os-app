"""
Imported bank balances (SECTION 9).

Uniqueness on (bank_account_id, balance_date, source) prevents accidental
duplicate balance imports for the same account/day/source, per SECTION 9's
explicit "prevent accidental duplicate balance imports" requirement.
"""
import datetime
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin, UUIDPKMixin


class BankBalance(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "bank_balances"
    __table_args__ = (
        UniqueConstraint(
            "bank_account_id", "balance_date", "source", name="uq_bank_balance_identity"
        ),
    )

    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False, index=True
    )
    balance_date: Mapped[datetime.date] = mapped_column(nullable=False, index=True)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    closing_balance: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    available_balance: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    ledger_balance: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    # Source traceability (SECTION 19)
    source: Mapped[str] = mapped_column(String(50), default="MANUAL", nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True, index=True
    )
