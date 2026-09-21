"""
Currency & FX Rate Administration.

PRINCIPLE 2: every relevant financial object has a currency.
Currencies are configuration data, never hard-coded (see SECTION 6 of spec).
FX rates are effective-dated, versioned and never overwritten (PRINCIPLE 5).
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin


class Currency(Base, TimestampMixin, SoftDeleteMixin):
    """
    Currency master. Code (ISO 4217) is the natural primary key so foreign
    keys elsewhere read naturally (e.g. transaction_currency = 'USD').
    """
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(5), nullable=True)
    decimal_places: Mapped[int] = mapped_column(default=2, nullable=False)
    is_base_currency: Mapped[bool] = mapped_column(
        default=False, comment="Flag for currencies usable as a Group reporting currency"
    )


class FXRateType(str, Enum):
    SPOT = "SPOT"
    BUDGET = "BUDGET"
    MANAGEMENT = "MANAGEMENT"
    TREASURY = "TREASURY"
    HISTORICAL = "HISTORICAL"


class FXRate(Base, UUIDPKMixin, TimestampMixin):
    """
    Effective-dated, versioned FX rate. Historical rates are NEVER updated
    in place: a correction is a new row with a higher `version`, and the
    prior row is superseded, not deleted (PRINCIPLE 5).
    """
    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint(
            "from_currency_code", "to_currency_code", "rate_type", "rate_date", "version",
            name="uq_fx_rate_identity",
        ),
    )

    from_currency_code: Mapped[str] = mapped_column(String(3), ForeignKey("currencies.code"), nullable=False)
    to_currency_code: Mapped[str] = mapped_column(String(3), ForeignKey("currencies.code"), nullable=False)
    rate_type: Mapped[FXRateType] = mapped_column(nullable=False, default=FXRateType.SPOT)
    rate_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    rate_source: Mapped[str] = mapped_column(String(100), nullable=False, default="MANUAL")
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    is_current: Mapped[bool] = mapped_column(
        default=True, comment="True only for the latest version of this rate identity"
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fx_rates.id"), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
