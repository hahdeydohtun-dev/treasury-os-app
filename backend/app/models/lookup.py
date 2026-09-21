"""
Configuration/lookup tables shared by the Treasury Data Foundation.

Per the spec, bank account types and cash event types must be extensible
(admin-configurable), not a fixed enum baked into application code. These
follow the same pattern as `Currency`: a natural string code as primary
key, seeded with sensible defaults, extendable via the API without a
schema change.
"""
from enum import Enum

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin


class CashDirection(str, Enum):
    """
    The classification every cash event must ultimately resolve to
    (SECTION 6 of the Stage 2 spec). Stored on the transaction itself
    (not only derived from event type) so a given event type's typical
    direction can be overridden case-by-case without a data model change.
    """
    INFLOW = "INFLOW"
    OUTFLOW = "OUTFLOW"
    TRANSFER = "TRANSFER"
    NON_CASH = "NON_CASH"


class AccountType(Base, TimestampMixin, SoftDeleteMixin):
    """Bank account type master (Current, Savings, Escrow, ... extensible)."""
    __tablename__ = "account_types"

    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CashEventType(Base, TimestampMixin, SoftDeleteMixin):
    """
    Treasury transaction / cash event type master (BANK_RECEIPT,
    SUPPLIER_PAYMENT, INTERCOMPANY_RECEIPT, ... extensible per SECTION 4).
    `default_direction` seeds the classification on new transactions of
    this type, but each transaction stores its own resolved direction.
    """
    __tablename__ = "cash_event_types"

    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_direction: Mapped[CashDirection] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
