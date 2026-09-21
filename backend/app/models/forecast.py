"""
13-Week Cash Flow Forecast Engine domain models (Stage 3).

Design notes (see FORECAST_ENGINE.md for the full architecture writeup):

- A `Forecast` is one versioned run/snapshot (SECTION 5). Rolling forward
  (SECTION 4) creates a NEW Forecast row linked via `parent_forecast_id`
  rather than mutating the prior one - historical forecasts are never
  overwritten, the same principle already applied to `FXRate`.
- `ForecastWeek` stores the *consolidated* (reporting-currency) waterfall
  per week for the forecast's scope. Entity-level and currency-level
  "views" (SECTION 6/7) are computed on demand by aggregating
  `ForecastLine` rows (which carry both the original transaction currency
  amount AND the converted reporting amount) - so a currency shortfall is
  never hidden by a healthy consolidated figure.
- `ForecastCategory` is a configurable hierarchy (SECTION 9), not a fixed
  enum, mirroring the `Currency`/`AccountType`/`CashEventType` pattern
  already used elsewhere.
- Forecast-vs-actual variance is intentionally NOT a persisted table: it
  is a derived read-model computed from `ForecastLine` (forecast) and
  `TreasuryTransaction` (actual) at query time (see
  app/services/forecast_variance_service.py), so it can never drift from
  its sources.
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin
from app.models.lookup import CashDirection


class ForecastScenarioType(str, Enum):
    BASE = "BASE"
    CONSERVATIVE = "CONSERVATIVE"
    STRESS = "STRESS"


class ForecastStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class ForecastValueBasis(str, Enum):
    """SECTION 10: the user must choose, not the system silently deciding."""
    GROSS = "GROSS"
    PROBABILITY_ADJUSTED = "PROBABILITY_ADJUSTED"


class WeekStatus(str, Enum):
    FUTURE = "FUTURE"
    CURRENT = "CURRENT"
    COMPLETED = "COMPLETED"


class ForecastSourceType(str, Enum):
    ACTUAL = "ACTUAL"
    EXPECTED_COLLECTION = "EXPECTED_COLLECTION"
    EXPECTED_PAYMENT = "EXPECTED_PAYMENT"
    RECURRING = "RECURRING"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
    SCENARIO_ADJUSTMENT = "SCENARIO_ADJUSTMENT"
    FACILITY_DRAWDOWN = "FACILITY_DRAWDOWN"
    FACILITY_REPAYMENT = "FACILITY_REPAYMENT"
    FACILITY_INTEREST = "FACILITY_INTEREST"
    FACILITY_FEE = "FACILITY_FEE"
    INVESTMENT_PLACEMENT = "INVESTMENT_PLACEMENT"
    INVESTMENT_MATURITY_PRINCIPAL = "INVESTMENT_MATURITY_PRINCIPAL"
    INVESTMENT_MATURITY_INTEREST = "INVESTMENT_MATURITY_INTEREST"
    INVESTMENT_INTEREST_RECEIPT = "INVESTMENT_INTEREST_RECEIPT"
    INVESTMENT_TERMINATION = "INVESTMENT_TERMINATION"
    FUTURE_LOAN = "FUTURE_LOAN"
    FUTURE_INVESTMENT = "FUTURE_INVESTMENT"
    FUTURE_INTERCOMPANY = "FUTURE_INTERCOMPANY"
    OTHER = "OTHER"


class RecurringFrequency(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    CUSTOM = "CUSTOM"


class LiquidityScopeType(str, Enum):
    GROUP = "GROUP"
    ENTITY = "ENTITY"
    CURRENCY = "CURRENCY"
    BANK_ACCOUNT = "BANK_ACCOUNT"


class AlertSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class AdjustmentStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"


class ForecastCategory(Base, TimestampMixin, SoftDeleteMixin):
    """Configurable cash-flow category hierarchy (SECTION 9)."""
    __tablename__ = "forecast_categories"

    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[CashDirection] = mapped_column(nullable=False)
    parent_code: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("forecast_categories.code"), nullable=True
    )
    forecastable: Mapped[bool] = mapped_column(default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Forecast(Base, UUIDPKMixin, TimestampMixin):
    """
    One forecast run/version (SECTION 5). `legal_entity_id` null means a
    group-wide forecast (lines may span multiple entities); set means the
    forecast is scoped to that entity only.
    """
    __tablename__ = "forecasts"

    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=True, index=True
    )
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=True, index=True
    )
    forecast_start_date: Mapped[datetime.date] = mapped_column(nullable=False)
    forecast_end_date: Mapped[datetime.date] = mapped_column(nullable=False)
    scenario: Mapped[ForecastScenarioType] = mapped_column(
        default=ForecastScenarioType.BASE, nullable=False, index=True
    )
    value_basis: Mapped[ForecastValueBasis] = mapped_column(
        default=ForecastValueBasis.PROBABILITY_ADJUSTED, nullable=False
    )
    reporting_currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    parent_forecast_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("forecasts.id"), nullable=True
    )
    status: Mapped[ForecastStatus] = mapped_column(
        default=ForecastStatus.DRAFT, nullable=False, index=True
    )
    assumptions_notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    data_quality_warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    opening_cash_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict,
        comment="Traceable opening cash used for week 1, keyed 'entity_id|currency_code' "
                "-> amount, captured from the Cash Position service at calculation time.",
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    weeks: Mapped[list["ForecastWeek"]] = relationship(
        back_populates="forecast", cascade="all, delete-orphan", order_by="ForecastWeek.week_number"
    )
    lines: Mapped[list["ForecastLine"]] = relationship(
        back_populates="forecast", cascade="all, delete-orphan"
    )
    assumptions: Mapped[list["ForecastScenarioAssumption"]] = relationship(
        back_populates="forecast", cascade="all, delete-orphan"
    )


class ForecastWeek(Base, UUIDPKMixin, TimestampMixin):
    """
    Consolidated (reporting-currency) weekly waterfall for the forecast's
    scope (SECTION 13/24). Per-entity/per-currency figures are derived
    from ForecastLine on demand, not duplicated here.
    """
    __tablename__ = "forecast_weeks"

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("forecasts.id"), nullable=False, index=True
    )
    week_number: Mapped[int] = mapped_column(nullable=False)
    start_date: Mapped[datetime.date] = mapped_column(nullable=False)
    end_date: Mapped[datetime.date] = mapped_column(nullable=False)
    status: Mapped[WeekStatus] = mapped_column(default=WeekStatus.FUTURE, nullable=False)

    opening_cash: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    total_inflows: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    total_outflows: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    net_transfers: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    net_cash_flow: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    closing_cash: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    minimum_required_liquidity: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), default=0, nullable=False
    )
    surplus_or_gap: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    liquidity_available: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    liquidity_required: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    liquidity_coverage_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), nullable=True
    )

    forecast: Mapped["Forecast"] = relationship(back_populates="weeks")


class ForecastLine(Base, UUIDPKMixin, TimestampMixin):
    """
    A single cash-flow driver contributing to a forecast week (SECTION 23).
    Traceable: `source_type` + `source_id` point back to the originating
    ExpectedCollection/ExpectedPayment/RecurringCashFlow/ForecastAdjustment/
    TreasuryTransaction row, answering "why is this amount in the forecast?"
    """
    __tablename__ = "forecast_lines"

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("forecasts.id"), nullable=False, index=True
    )
    week_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("forecast_weeks.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    category_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("forecast_categories.code"), nullable=False, index=True
    )
    direction: Mapped[CashDirection] = mapped_column(nullable=False)

    transaction_currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False, index=True
    )
    original_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    probability: Mapped[int | None] = mapped_column(nullable=True)
    adjusted_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, comment="original_amount after the forecast's value_basis"
    )
    reporting_currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    reporting_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    fx_rate_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fx_rate_date: Mapped[datetime.date | None] = mapped_column(nullable=True)

    source_type: Mapped[ForecastSourceType] = mapped_column(nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    counterparty: Mapped[str | None] = mapped_column(String(255), nullable=True)

    forecast: Mapped["Forecast"] = relationship(back_populates="lines")


class ForecastScenarioAssumption(Base, UUIDPKMixin, TimestampMixin):
    """
    Configurable scenario assumptions (SECTION 16-19) - never hard-coded
    percentages in Python. `assumption_type` is a free-form code (e.g.
    'COLLECTION_DELAY_WEEKS', 'COLLECTION_PROBABILITY_HAIRCUT_PCT',
    'PAYMENT_ACCELERATION_WEEKS', 'UNEXPECTED_OUTFLOW_AMOUNT',
    'MIN_CASH_BUFFER_MULTIPLIER') interpreted by the forecast engine.
    """
    __tablename__ = "forecast_scenario_assumptions"

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("forecasts.id"), nullable=False, index=True
    )
    assumption_type: Mapped[str] = mapped_column(String(100), nullable=False)
    numeric_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    category_code: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("forecast_categories.code"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    forecast: Mapped["Forecast"] = relationship(back_populates="assumptions")


class ForecastAdjustment(Base, UUIDPKMixin, TimestampMixin):
    """
    Manual forecast adjustment layer (SECTION 20). Never modifies the
    source ExpectedCollection/ExpectedPayment/RecurringCashFlow record -
    this is an additive line applied at the next recalculation.
    """
    __tablename__ = "forecast_adjustments"

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("forecasts.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    week_number: Mapped[int] = mapped_column(nullable=False)
    category_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("forecast_categories.code"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    direction: Mapped[CashDirection] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[AdjustmentStatus] = mapped_column(
        default=AdjustmentStatus.APPLIED, nullable=False
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class RecurringCashFlow(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    """Recurring forecast assumption (SECTION 21) - never generates actual transactions."""
    __tablename__ = "recurring_cash_flows"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    category_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("forecast_categories.code"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    direction: Mapped[CashDirection] = mapped_column(nullable=False)
    start_date: Mapped[datetime.date] = mapped_column(nullable=False)
    end_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    frequency: Mapped[RecurringFrequency] = mapped_column(nullable=False)
    custom_interval_days: Mapped[int | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class LiquidityThreshold(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    """Configurable minimum liquidity requirements (SECTION 14)."""
    __tablename__ = "liquidity_thresholds"

    scope_type: Mapped[LiquidityScopeType] = mapped_column(nullable=False, index=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=True, index=True
    )
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True, index=True)
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=True
    )
    minimum_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)


class ForecastAlert(Base, UUIDPKMixin, TimestampMixin):
    """Liquidity/variance alert generated during forecast calculation (SECTION 28)."""
    __tablename__ = "forecast_alerts"

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("forecasts.id"), nullable=False, index=True
    )
    severity: Mapped[AlertSeverity] = mapped_column(nullable=False, index=True)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=True
    )
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    week_number: Mapped[int | None] = mapped_column(nullable=True)
    metric: Mapped[str] = mapped_column(String(100), nullable=False)
    threshold_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    actual_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[AlertStatus] = mapped_column(default=AlertStatus.OPEN, nullable=False, index=True)
