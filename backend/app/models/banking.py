"""
Banks & Bank Accounts (SECTION 7).

A Bank is shared master data (a real-world banking institution) — it is
not owned by a single legal entity, since several entities in a group can
bank with the same institution. A BankAccount is what's entity-scoped,
currency-scoped, and account-type-scoped.
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin


class BankAccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    CLOSED = "CLOSED"


class Bank(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "banks"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    swift_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    accounts: Mapped[list["BankAccount"]] = relationship(back_populates="bank")


class BankAccount(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "bank_accounts"

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    bank_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("banks.id"), nullable=False, index=True
    )
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Full account number is stored for reconciliation matching; API responses
    # should mask it (see schemas.banking) - never log or display it raw.
    account_number: Mapped[str] = mapped_column(String(100), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    account_type_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("account_types.code"), nullable=False
    )
    status: Mapped[BankAccountStatus] = mapped_column(
        default=BankAccountStatus.ACTIVE, nullable=False
    )
    opening_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    closing_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    minimum_operating_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True
    )
    overdraft_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    gl_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)

    bank: Mapped["Bank"] = relationship(back_populates="accounts")

    __table_args__ = (
        UniqueConstraint("bank_id", "account_number", name="uq_bank_account_number"),
    )
