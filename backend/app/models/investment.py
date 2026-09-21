"""
Investments & Fixed Deposit Management domain models (Stage 4).

Mirrors the Stage 3 Facility pattern deliberately, since it is the
established, already-hardened convention in this codebase:

- `Investment` is the master record (like `Facility`); its mutable
  commercial terms are never edited in place - a change creates a new
  `InvestmentVersion` row (like `FacilityVersion`).
- `InvestmentTransaction` is the financial-event ledger (like
  `FacilityDrawdown`/`FacilityRepayment`) - the investment master is
  NEVER itself the cash movement (SECTION 10).
- `InvestmentEvent` is the append-only, business-readable timeline (like
  `FacilityEvent`), distinct from the general `AuditEvent` table.
- Every monetary field is `Numeric` (Decimal), never float.
- Lifecycle transitions are an explicit table
  (`INVESTMENT_STATUS_TRANSITIONS`), the same "no arbitrary state
  changes" discipline as `FACILITY_STATUS_TRANSITIONS`.
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin


class InvestmentStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    PLACEMENT_PENDING = "PLACEMENT_PENDING"
    ACTIVE = "ACTIVE"
    MATURED = "MATURED"
    PARTIALLY_TERMINATED = "PARTIALLY_TERMINATED"
    TERMINATED = "TERMINATED"
    ROLLED_OVER = "ROLLED_OVER"
    REBOOKED = "REBOOKED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


# SECTION 8/9: an explicit transition table - never an arbitrary status
# change. PARTIALLY_TERMINATED stays ACTIVE-like (can still mature,
# terminate further, or roll over) since principal remains outstanding.
INVESTMENT_STATUS_TRANSITIONS: dict = {
    InvestmentStatus.DRAFT: {InvestmentStatus.SUBMITTED, InvestmentStatus.CANCELLED},
    InvestmentStatus.SUBMITTED: {
        InvestmentStatus.UNDER_REVIEW, InvestmentStatus.REJECTED, InvestmentStatus.CANCELLED,
    },
    InvestmentStatus.UNDER_REVIEW: {
        InvestmentStatus.APPROVED, InvestmentStatus.REJECTED, InvestmentStatus.CANCELLED,
    },
    InvestmentStatus.APPROVED: {
        InvestmentStatus.PLACEMENT_PENDING, InvestmentStatus.CANCELLED,
    },
    InvestmentStatus.PLACEMENT_PENDING: {InvestmentStatus.ACTIVE, InvestmentStatus.CANCELLED},
    InvestmentStatus.ACTIVE: {
        InvestmentStatus.MATURED, InvestmentStatus.PARTIALLY_TERMINATED,
        InvestmentStatus.TERMINATED, InvestmentStatus.ROLLED_OVER, InvestmentStatus.REBOOKED,
    },
    InvestmentStatus.PARTIALLY_TERMINATED: {
        InvestmentStatus.MATURED, InvestmentStatus.TERMINATED, InvestmentStatus.ROLLED_OVER,
        InvestmentStatus.REBOOKED,
    },
    InvestmentStatus.MATURED: {InvestmentStatus.TERMINATED, InvestmentStatus.ROLLED_OVER},
    InvestmentStatus.TERMINATED: set(),
    InvestmentStatus.ROLLED_OVER: set(),
    InvestmentStatus.REBOOKED: set(),
    InvestmentStatus.CANCELLED: set(),
    InvestmentStatus.REJECTED: set(),
}


class InvestmentRateType(str, Enum):
    FIXED = "FIXED"
    VARIABLE = "VARIABLE"
    NEGOTIATED = "NEGOTIATED"
    CUSTOM = "CUSTOM"


class DayCountConvention(str, Enum):
    """Re-declared here (rather than imported from app.models.facility) so
    the investments module has no hard dependency on the facilities
    module - the two happen to share the same three conventions, but
    Stage 4 is deliberately not coupled to Stage 3's enum lifecycle."""
    ACT_365 = "ACT_365"
    ACT_360 = "ACT_360"
    THIRTY_360 = "THIRTY_360"


class InterestPaymentMethod(str, Enum):
    AT_MATURITY = "AT_MATURITY"
    PERIODIC = "PERIODIC"
    UPFRONT = "UPFRONT"


class InterestPaymentFrequency(str, Enum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    SEMI_ANNUAL = "SEMI_ANNUAL"
    ANNUAL = "ANNUAL"
    CUSTOM = "CUSTOM"


class PenaltyType(str, Enum):
    NONE = "NONE"
    RATE_REDUCTION = "RATE_REDUCTION"
    FLAT_AMOUNT = "FLAT_AMOUNT"
    FORFEIT_INTEREST = "FORFEIT_INTEREST"
    CUSTOM = "CUSTOM"


class InvestmentTransactionType(str, Enum):
    """SECTION 10: the investment master is never itself the cash
    movement - every financial event is one of these explicit rows."""
    PLACEMENT = "PLACEMENT"
    INTEREST_ACCRUAL = "INTEREST_ACCRUAL"
    INTEREST_RECEIPT = "INTEREST_RECEIPT"
    PARTIAL_TERMINATION = "PARTIAL_TERMINATION"
    FULL_TERMINATION = "FULL_TERMINATION"
    ROLLOVER = "ROLLOVER"
    REBOOKING = "REBOOKING"
    PENALTY = "PENALTY"
    MATURITY_SETTLEMENT = "MATURITY_SETTLEMENT"


class InvestmentTransactionStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class InvestmentEventType(str, Enum):
    INVESTMENT_CREATED = "INVESTMENT_CREATED"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PLACED = "PLACED"
    TERMS_CHANGED = "TERMS_CHANGED"
    INTEREST_ACCRUED = "INTEREST_ACCRUED"
    INTEREST_RECEIVED = "INTEREST_RECEIVED"
    PARTIAL_TERMINATION = "PARTIAL_TERMINATION"
    FULL_TERMINATION = "FULL_TERMINATION"
    MATURED = "MATURED"
    ROLLOVER = "ROLLOVER"
    REBOOKED = "REBOOKED"
    PENALTY_APPLIED = "PENALTY_APPLIED"
    CANCELLED = "CANCELLED"
    STATUS_CHANGED = "STATUS_CHANGED"


class InvestmentType(Base, TimestampMixin, SoftDeleteMixin):
    """Configurable investment type lookup (SECTION 3) - not a fixed enum.
    Only FIXED_DEPOSIT is functionally implemented in Stage 4; the other
    seeded rows exist so the architecture is visibly extensible without
    implementing fake functionality for them (SECTION 3/45)."""
    __tablename__ = "investment_types"

    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_implemented: Mapped[bool] = mapped_column(default=False, nullable=False)


class Investment(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "investments"

    investment_reference: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    investment_type_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("investment_types.code"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business_units.id"), nullable=True
    )
    institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("banks.id"), nullable=False, index=True
    )
    source_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=True
    )
    destination_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=True
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )

    principal_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    original_principal_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)

    placement_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    value_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    start_date: Mapped[datetime.date] = mapped_column(nullable=False)
    maturity_date: Mapped[datetime.date] = mapped_column(nullable=False, index=True)
    tenor_days: Mapped[int] = mapped_column(nullable=False)

    interest_rate: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    rate_type: Mapped[InvestmentRateType] = mapped_column(
        default=InvestmentRateType.FIXED, nullable=False
    )
    rate_source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    day_count_convention: Mapped[DayCountConvention] = mapped_column(
        SAEnum(DayCountConvention, name="investment_day_count_convention"),
        default=DayCountConvention.ACT_365, nullable=False,
    )
    interest_payment_method: Mapped[InterestPaymentMethod] = mapped_column(
        default=InterestPaymentMethod.AT_MATURITY, nullable=False
    )
    interest_payment_frequency: Mapped[InterestPaymentFrequency | None] = mapped_column(nullable=True)

    expected_interest: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    accrued_interest: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    received_interest: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)

    early_termination_allowed: Mapped[bool] = mapped_column(default=True, nullable=False)
    partial_termination_allowed: Mapped[bool] = mapped_column(default=True, nullable=False)
    rollover_allowed: Mapped[bool] = mapped_column(default=True, nullable=False)

    penalty_type: Mapped[PenaltyType] = mapped_column(default=PenaltyType.NONE, nullable=False)
    penalty_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    penalty_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    status: Mapped[InvestmentStatus] = mapped_column(
        default=InvestmentStatus.DRAFT, nullable=False, index=True
    )
    purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    version: Mapped[int] = mapped_column(default=1, nullable=False)
    # SECTION 16: explicit rollover lineage - never inferred, never
    # silently overwriting the original investment's own terms.
    previous_investment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investments.id"), nullable=True
    )
    rolled_to_investment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investments.id"), nullable=True
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    placed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class InvestmentVersion(Base, UUIDPKMixin, TimestampMixin):
    """Effective-dated snapshot of an investment's commercial terms
    (SECTION 4/18) - inserted on every material change, never overwritten."""
    __tablename__ = "investment_versions"

    investment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investments.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(nullable=False)
    effective_date: Mapped[datetime.date] = mapped_column(nullable=False)
    terms: Mapped[dict] = mapped_column(JSONB, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class InvestmentTransaction(Base, UUIDPKMixin, TimestampMixin):
    """
    SECTION 10: the financial-event ledger. The Investment master
    represents the instrument; rows here represent actual financial
    activity (placement, interest accrual/receipt, termination,
    rollover, rebooking, penalty, maturity settlement).
    """
    __tablename__ = "investment_transactions"

    investment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investments.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    transaction_type: Mapped[InvestmentTransactionType] = mapped_column(nullable=False, index=True)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    transaction_date: Mapped[datetime.date] = mapped_column(nullable=False, index=True)
    status: Mapped[InvestmentTransactionStatus] = mapped_column(
        default=InvestmentTransactionStatus.DRAFT, nullable=False
    )
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # For a rollover/rebooking transaction, links the new investment created.
    related_investment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investments.id"), nullable=True
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    executed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(50), default="MANUAL", nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True
    )


class InvestmentEvent(Base, UUIDPKMixin, TimestampMixin):
    """Append-only, business-readable investment lifecycle timeline
    (SECTION 19) - distinct from the general AuditEvent table."""
    __tablename__ = "investment_events"

    investment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investments.id"), nullable=False, index=True
    )
    event_type: Mapped[InvestmentEventType] = mapped_column(nullable=False, index=True)
    event_date: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    previous_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    related_record_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    related_record_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class InvestmentConcentrationLimit(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    """
    SECTION 23: configurable concentration limits - never hard-coded
    regulatory limits. A limit applies to one dimension
    (institution/entity/currency/investment type) via exactly one of the
    optional foreign keys/codes below being set.
    """
    __tablename__ = "investment_concentration_limits"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=True, index=True
    )
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=True, index=True
    )
    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("banks.id"), nullable=True, index=True
    )
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    investment_type_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    limit_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    warning_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
