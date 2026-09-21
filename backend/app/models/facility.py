"""
Funding & Credit Facilities domain models (Stage 3).

Design notes (full detail in docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md):

- Facility is the master record; its mutable commercial terms (limit,
  rate, maturity, fees, ...) are never edited in place - a change creates
  a new FacilityVersion row (SECTION 8), the same "never overwrite
  historical financial terms" principle already applied to FXRate.
- available_amount is NOT assumed to equal committed_limit -
  drawn_amount (SECTION 1/14). It's computed explicitly (see
  app/services/facility_engine.py) accounting for sub-limits and
  covenant/borrowing-base restrictions.
- Every monetary field is Numeric (Decimal), never float (SECTION 4).
- Every facility belongs to exactly one LegalEntity (PRINCIPLE 1) and
  carries its own currency, never silently converted (SECTION 6).
- FacilityEvent is the append-only lifecycle/audit trail (SECTION 22),
  distinct from the general AuditEvent table - a business-readable
  timeline, not a technical log.
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin


class CommitmentType(str, Enum):
    COMMITTED = "COMMITTED"
    UNCOMMITTED = "UNCOMMITTED"


class FacilityStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"
    MATURED = "MATURED"
    RENEWAL_PENDING = "RENEWAL_PENDING"
    RENEWED = "RENEWED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


FACILITY_STATUS_TRANSITIONS: dict = {
    FacilityStatus.DRAFT: {FacilityStatus.ACTIVE, FacilityStatus.CANCELLED},
    FacilityStatus.ACTIVE: {
        FacilityStatus.SUSPENDED, FacilityStatus.EXPIRED, FacilityStatus.MATURED,
        FacilityStatus.RENEWAL_PENDING, FacilityStatus.CLOSED,
    },
    FacilityStatus.SUSPENDED: {FacilityStatus.ACTIVE, FacilityStatus.CLOSED, FacilityStatus.CANCELLED},
    FacilityStatus.RENEWAL_PENDING: {FacilityStatus.RENEWED, FacilityStatus.EXPIRED, FacilityStatus.CLOSED},
    FacilityStatus.RENEWED: {FacilityStatus.ACTIVE},
    FacilityStatus.MATURED: {FacilityStatus.CLOSED, FacilityStatus.RENEWAL_PENDING},
    FacilityStatus.EXPIRED: {FacilityStatus.CLOSED},
    FacilityStatus.CLOSED: set(),
    FacilityStatus.CANCELLED: set(),
}


class InterestRateType(str, Enum):
    FIXED = "FIXED"
    VARIABLE = "VARIABLE"
    BENCHMARK_PLUS_SPREAD = "BENCHMARK_PLUS_SPREAD"
    CUSTOM = "CUSTOM"


class DayCountConvention(str, Enum):
    ACT_365 = "ACT_365"
    ACT_360 = "ACT_360"
    THIRTY_360 = "THIRTY_360"


class RepaymentMethod(str, Enum):
    EQUAL_PRINCIPAL = "EQUAL_PRINCIPAL"
    EQUAL_INSTALLMENT = "EQUAL_INSTALLMENT"
    INTEREST_ONLY = "INTEREST_ONLY"
    BULLET = "BULLET"
    CUSTOM_SCHEDULE = "CUSTOM_SCHEDULE"


class DrawdownStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RepaymentStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    DUE = "DUE"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"


class RepaymentType(str, Enum):
    PRINCIPAL = "PRINCIPAL"
    INTEREST = "INTEREST"
    FEE = "FEE"


class FeeType(str, Enum):
    ARRANGEMENT = "ARRANGEMENT"
    COMMITMENT = "COMMITMENT"
    PROCESSING = "PROCESSING"
    RENEWAL = "RENEWAL"
    EARLY_REPAYMENT = "EARLY_REPAYMENT"
    OTHER = "OTHER"


class FeeStatus(str, Enum):
    DUE = "DUE"
    PAID = "PAID"
    WAIVED = "WAIVED"
    CANCELLED = "CANCELLED"


class CovenantType(str, Enum):
    MINIMUM_LIQUIDITY = "MINIMUM_LIQUIDITY"
    MAXIMUM_LEVERAGE = "MAXIMUM_LEVERAGE"
    DEBT_SERVICE_COVERAGE = "DEBT_SERVICE_COVERAGE"
    INTEREST_COVERAGE = "INTEREST_COVERAGE"
    CURRENT_RATIO = "CURRENT_RATIO"
    MINIMUM_NET_WORTH = "MINIMUM_NET_WORTH"
    BORROWING_BASE_AVAILABILITY = "BORROWING_BASE_AVAILABILITY"
    CUSTOM = "CUSTOM"


class CovenantOperator(str, Enum):
    GREATER_THAN_OR_EQUAL = "GTE"
    LESS_THAN_OR_EQUAL = "LTE"
    GREATER_THAN = "GT"
    LESS_THAN = "LT"
    EQUAL = "EQ"


class CovenantStatus(str, Enum):
    COMPLIANT = "COMPLIANT"
    WARNING = "WARNING"
    BREACH = "BREACH"
    WAIVED = "WAIVED"
    CURED = "CURED"
    UNKNOWN = "UNKNOWN"
    DATA_REQUIRED = "DATA_REQUIRED"


class CollateralType(str, Enum):
    CASH_COLLATERAL = "CASH_COLLATERAL"
    FIXED_DEPOSIT = "FIXED_DEPOSIT"
    RECEIVABLES = "RECEIVABLES"
    INVENTORY = "INVENTORY"
    PROPERTY = "PROPERTY"
    EQUIPMENT = "EQUIPMENT"
    GUARANTEE = "GUARANTEE"
    OTHER = "OTHER"


class CollateralStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class FacilityEventType(str, Enum):
    FACILITY_CREATED = "FACILITY_CREATED"
    FACILITY_ACTIVATED = "FACILITY_ACTIVATED"
    DRAW_DOWN = "DRAW_DOWN"
    REPAYMENT = "REPAYMENT"
    EARLY_REPAYMENT = "EARLY_REPAYMENT"
    RATE_CHANGED = "RATE_CHANGED"
    LIMIT_CHANGED = "LIMIT_CHANGED"
    MATURITY_EXTENDED = "MATURITY_EXTENDED"
    RENEWED = "RENEWED"
    REFINANCED = "REFINANCED"
    FEE_CHARGED = "FEE_CHARGED"
    COVENANT_WARNING = "COVENANT_WARNING"
    COVENANT_BREACH = "COVENANT_BREACH"
    COLLATERAL_ADDED = "COLLATERAL_ADDED"
    COLLATERAL_RELEASED = "COLLATERAL_RELEASED"
    FACILITY_SUSPENDED = "FACILITY_SUSPENDED"
    FACILITY_CLOSED = "FACILITY_CLOSED"
    STATUS_CHANGED = "STATUS_CHANGED"


class FundingActionType(str, Enum):
    PROPOSE_DRAWDOWN = "PROPOSE_DRAWDOWN"
    PROPOSE_REPAYMENT = "PROPOSE_REPAYMENT"
    PROPOSE_REFINANCING = "PROPOSE_REFINANCING"
    PROPOSE_RENEWAL = "PROPOSE_RENEWAL"
    PROPOSE_LIMIT_CHANGE = "PROPOSE_LIMIT_CHANGE"


class FundingActionStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# SECTION 27: "DRAFT -> SUBMITTED -> UNDER_REVIEW -> APPROVED -> EXECUTED ->
# COMPLETED" plus REJECTED/CANCELLED - an explicit table, same "no
# arbitrary state changes" discipline as FACILITY_STATUS_TRANSITIONS.
FUNDING_ACTION_STATUS_TRANSITIONS: dict = {
    FundingActionStatus.DRAFT: {FundingActionStatus.SUBMITTED, FundingActionStatus.CANCELLED},
    FundingActionStatus.SUBMITTED: {
        FundingActionStatus.UNDER_REVIEW, FundingActionStatus.REJECTED, FundingActionStatus.CANCELLED,
    },
    FundingActionStatus.UNDER_REVIEW: {
        FundingActionStatus.APPROVED, FundingActionStatus.REJECTED, FundingActionStatus.CANCELLED,
    },
    FundingActionStatus.APPROVED: {FundingActionStatus.EXECUTED, FundingActionStatus.CANCELLED},
    FundingActionStatus.EXECUTED: {FundingActionStatus.COMPLETED},
    FundingActionStatus.COMPLETED: set(),
    FundingActionStatus.REJECTED: set(),
    FundingActionStatus.CANCELLED: set(),
}


class FacilityType(Base, TimestampMixin, SoftDeleteMixin):
    """Configurable facility type lookup (SECTION 2) - not a fixed enum."""
    __tablename__ = "facility_types"

    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Facility(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "facilities"

    facility_reference: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    facility_name: Mapped[str] = mapped_column(String(255), nullable=False)
    facility_type_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("facility_types.code"), nullable=False, index=True
    )
    commitment_type: Mapped[CommitmentType] = mapped_column(nullable=False, index=True)

    lender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("banks.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )

    approved_limit: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    committed_limit: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    current_drawn_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    covenant_restricted_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), default=0, nullable=False,
        comment="Amount of committed_limit made unavailable by covenant/borrowing-base "
                "restrictions - see facility_engine.calculate_utilization.",
    )

    interest_rate_type: Mapped[InterestRateType] = mapped_column(nullable=False)
    benchmark_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    spread: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    fixed_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    day_count_convention: Mapped[DayCountConvention] = mapped_column(
        default=DayCountConvention.ACT_365, nullable=False
    )

    arrangement_fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    commitment_fee_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    processing_fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    start_date: Mapped[datetime.date] = mapped_column(nullable=False)
    availability_start_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    availability_end_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    maturity_date: Mapped[datetime.date] = mapped_column(nullable=False, index=True)
    renewal_date: Mapped[datetime.date | None] = mapped_column(nullable=True)

    repayment_frequency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    repayment_method: Mapped[RepaymentMethod] = mapped_column(
        default=RepaymentMethod.BULLET, nullable=False
    )
    next_repayment_date: Mapped[datetime.date | None] = mapped_column(nullable=True)

    status: Mapped[FacilityStatus] = mapped_column(
        default=FacilityStatus.DRAFT, nullable=False, index=True
    )
    purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)
    security_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    collateral_description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    version: Mapped[int] = mapped_column(default=1, nullable=False)
    refinanced_from_facility_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=True,
        comment="SECTION 21: links a refinancing facility back to the one it replaced.",
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class FacilityVersion(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "facility_versions"

    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(nullable=False)
    effective_date: Mapped[datetime.date] = mapped_column(nullable=False)
    terms: Mapped[dict] = mapped_column(JSONB, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class FacilitySubLimit(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "facility_sub_limits"

    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    drawn_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)


class FacilityDrawdown(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "facility_drawdowns"

    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    sub_limit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facility_sub_limits.id"), nullable=True
    )
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    drawdown_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    drawdown_date: Mapped[datetime.date] = mapped_column(nullable=False, index=True)
    value_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    maturity_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    interest_rate_applicable: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    status: Mapped[DrawdownStatus] = mapped_column(default=DrawdownStatus.DRAFT, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    executed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    source_type: Mapped[str] = mapped_column(String(50), default="MANUAL", nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    source_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True
    )


class FacilityRepayment(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "facility_repayments"

    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    drawdown_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facility_drawdowns.id"), nullable=True
    )
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    repayment_type: Mapped[RepaymentType] = mapped_column(nullable=False)
    original_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    due_date: Mapped[datetime.date] = mapped_column(nullable=False, index=True)
    actual_payment_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    status: Mapped[RepaymentStatus] = mapped_column(default=RepaymentStatus.SCHEDULED, nullable=False)
    is_early_repayment: Mapped[bool] = mapped_column(default=False, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(50), default="MANUAL", nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    source_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True
    )


class FacilityFee(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "facility_fees"

    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    fee_type: Mapped[FeeType] = mapped_column(nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    calculation_basis: Mapped[str | None] = mapped_column(String(200), nullable=True)
    due_date: Mapped[datetime.date] = mapped_column(nullable=False)
    paid_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    status: Mapped[FeeStatus] = mapped_column(default=FeeStatus.DUE, nullable=False)

    source_type: Mapped[str] = mapped_column(String(50), default="MANUAL", nullable=False)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True
    )


class FacilityCovenant(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "facility_covenants"

    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    covenant_type: Mapped[CovenantType] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    operator: Mapped[CovenantOperator] = mapped_column(nullable=False)
    measurement_frequency: Mapped[str | None] = mapped_column(String(50), nullable=True)
    measurement_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    headroom: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    status: Mapped[CovenantStatus] = mapped_column(default=CovenantStatus.DATA_REQUIRED, nullable=False)
    warning_threshold: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    breach_threshold: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    cure_period_days: Mapped[int | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class FacilityCollateral(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "facility_collateral"

    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=False, index=True
    )
    collateral_type: Mapped[CollateralType] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    value: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    valuation_date: Mapped[datetime.date] = mapped_column(nullable=False)
    haircut_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=0, nullable=False)
    expiry_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    document_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    status: Mapped[CollateralStatus] = mapped_column(default=CollateralStatus.ACTIVE, nullable=False)

    @property
    def eligible_value(self) -> Decimal:
        return (self.value * (Decimal(1) - self.haircut_pct / Decimal(100))).quantize(Decimal("0.01"))


class FacilityEvent(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "facility_events"

    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=False, index=True
    )
    event_type: Mapped[FacilityEventType] = mapped_column(nullable=False, index=True)
    event_date: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    previous_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    related_record_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    related_record_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class FundingAction(Base, UUIDPKMixin, TimestampMixin):
    """
    A proposed funding decision/workflow record (SECTION 27). This is
    NEVER itself the financial transaction. `FacilityDrawdown` and
    `FacilityRepayment` remain the sole financial transaction records;
    a `FundingAction` may optionally point at ONE specific drawdown or
    repayment it is meant to authorize (`linked_drawdown_id` /
    `linked_repayment_id` - explicit foreign keys, never a free-text or
    implicit relationship). The action's own status reaching EXECUTED
    does not, by itself, mean the underlying transaction happened - see
    app/api/v1/funding.py::execute_funding_action, which refuses to mark
    a linked action EXECUTED until the linked drawdown/repayment's own
    status shows it was actually executed/paid (in full - a partially
    paid repayment does not qualify, a documented decision).

    Data-integrity rules (app/services/funding_action_validation.py,
    enforced at the API layer AND, for the two structural rules below,
    at the database level as defense in depth):
      - at most one of linked_drawdown_id / linked_repayment_id may be
        set (CheckConstraint below);
      - each drawdown/repayment may be linked from at most one
        FundingAction (partial unique indexes - see the migration that
        added these columns);
      - action_type must match the kind of link (PROPOSE_DRAWDOWN <->
        linked_drawdown_id, PROPOSE_REPAYMENT <-> linked_repayment_id);
      - a linked transaction's entity/facility must match this action's
        own legal_entity_id/facility_id; the linked facility must belong
        to legal_entity_id; and, if this action specifies amount/
        currency at all, they must match the linked transaction's values
        exactly (an action with no amount/currency simply defers to the
        transaction's own recorded values).
    """
    __tablename__ = "funding_actions"
    __table_args__ = (
        CheckConstraint(
            "NOT (linked_drawdown_id IS NOT NULL AND linked_repayment_id IS NOT NULL)",
            name="ck_funding_action_single_link",
        ),
    )

    action_type: Mapped[FundingActionType] = mapped_column(nullable=False, index=True)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facilities.id"), nullable=True, index=True
    )
    linked_drawdown_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facility_drawdowns.id"), nullable=True, index=True
    )
    linked_repayment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("facility_repayments.id"), nullable=True, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    proposed_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    status: Mapped[FundingActionStatus] = mapped_column(
        default=FundingActionStatus.DRAFT, nullable=False, index=True
    )
    rationale: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    executed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
