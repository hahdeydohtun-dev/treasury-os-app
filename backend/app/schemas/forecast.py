import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.forecast import (
    AdjustmentStatus,
    AlertSeverity,
    AlertStatus,
    ForecastScenarioType,
    ForecastSourceType,
    ForecastStatus,
    ForecastValueBasis,
    LiquidityScopeType,
    RecurringFrequency,
    WeekStatus,
)
from app.models.lookup import CashDirection


class ForecastCreate(BaseModel):
    group_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID | None = None
    forecast_start_date: datetime.date
    scenario: ForecastScenarioType = ForecastScenarioType.BASE
    value_basis: ForecastValueBasis = ForecastValueBasis.PROBABILITY_ADJUSTED
    reporting_currency_code: str = Field(min_length=3, max_length=3)
    assumptions_notes: str | None = None


class ForecastOut(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID | None
    legal_entity_id: uuid.UUID | None
    forecast_start_date: datetime.date
    forecast_end_date: datetime.date
    scenario: ForecastScenarioType
    value_basis: ForecastValueBasis
    reporting_currency_code: str
    version: int
    parent_forecast_id: uuid.UUID | None
    status: ForecastStatus
    assumptions_notes: str | None
    data_quality_warnings: list[str]
    created_by_user_id: uuid.UUID | None
    published_by_user_id: uuid.UUID | None
    published_at: datetime.datetime | None

    model_config = {"from_attributes": True}


class ForecastWeekOut(BaseModel):
    week_number: int
    start_date: datetime.date
    end_date: datetime.date
    status: WeekStatus
    opening_cash: Decimal
    total_inflows: Decimal
    total_outflows: Decimal
    net_transfers: Decimal
    net_cash_flow: Decimal
    closing_cash: Decimal
    minimum_required_liquidity: Decimal
    surplus_or_gap: Decimal
    liquidity_available: Decimal
    liquidity_required: Decimal
    liquidity_coverage_ratio: Decimal | None

    model_config = {"from_attributes": True}


class ForecastLineOut(BaseModel):
    id: uuid.UUID
    week_id: uuid.UUID
    legal_entity_id: uuid.UUID
    category_code: str
    direction: CashDirection
    transaction_currency_code: str
    original_amount: Decimal
    probability: int | None
    adjusted_amount: Decimal
    reporting_currency_code: str
    reporting_amount: Decimal
    fx_rate: Decimal | None
    fx_rate_type: str | None
    source_type: ForecastSourceType
    source_id: str | None
    description: str | None
    counterparty: str | None

    model_config = {"from_attributes": True}


class ForecastSummaryOut(BaseModel):
    forecast: ForecastOut
    weeks: list[ForecastWeekOut]
    opening_cash: Decimal
    thirteen_week_net_cash_flow: Decimal
    lowest_projected_cash: Decimal
    lowest_projected_cash_week: int | None
    largest_funding_gap: Decimal
    largest_funding_gap_week: int | None
    total_surplus: Decimal
    average_liquidity_coverage: Decimal | None


class ForecastCategoryCreate(BaseModel):
    code: str
    name: str
    type: CashDirection
    parent_code: str | None = None
    forecastable: bool = True
    description: str | None = None


class ForecastCategoryOut(ForecastCategoryCreate):
    is_active: bool

    model_config = {"from_attributes": True}


class ScenarioAssumptionCreate(BaseModel):
    assumption_type: str
    numeric_value: Decimal
    currency_code: str | None = None
    category_code: str | None = None
    description: str | None = None


class ScenarioAssumptionOut(ScenarioAssumptionCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class ForecastAdjustmentCreate(BaseModel):
    legal_entity_id: uuid.UUID
    currency_code: str = Field(min_length=3, max_length=3)
    week_number: int = Field(ge=1, le=13)
    category_code: str
    amount: Decimal
    direction: CashDirection
    reason: str


class ForecastAdjustmentOut(ForecastAdjustmentCreate):
    id: uuid.UUID
    forecast_id: uuid.UUID
    status: AdjustmentStatus
    created_by_user_id: uuid.UUID | None
    approved_by_user_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class RecurringCashFlowCreate(BaseModel):
    name: str
    legal_entity_id: uuid.UUID
    currency_code: str = Field(min_length=3, max_length=3)
    category_code: str
    amount: Decimal
    direction: CashDirection
    start_date: datetime.date
    end_date: datetime.date | None = None
    frequency: RecurringFrequency
    custom_interval_days: int | None = None
    notes: str | None = None


class RecurringCashFlowOut(RecurringCashFlowCreate):
    id: uuid.UUID
    is_active: bool

    model_config = {"from_attributes": True}


class LiquidityThresholdCreate(BaseModel):
    scope_type: LiquidityScopeType
    legal_entity_id: uuid.UUID | None = None
    currency_code: str | None = None
    bank_account_id: uuid.UUID | None = None
    minimum_amount: Decimal


class LiquidityThresholdOut(LiquidityThresholdCreate):
    id: uuid.UUID
    is_active: bool

    model_config = {"from_attributes": True}


class ForecastAlertOut(BaseModel):
    id: uuid.UUID
    severity: AlertSeverity
    legal_entity_id: uuid.UUID | None
    currency_code: str | None
    week_number: int | None
    metric: str
    threshold_value: Decimal | None
    actual_value: Decimal | None
    message: str
    status: AlertStatus

    model_config = {"from_attributes": True}


class VarianceRowOut(BaseModel):
    week_number: int | None
    legal_entity_id: uuid.UUID | None
    currency_code: str | None
    category_code: str | None
    forecast_amount: Decimal
    actual_amount: Decimal
    variance_amount: Decimal
    variance_percentage: Decimal | None


class AccuracyOut(BaseModel):
    overall_accuracy: Decimal | None
    inflow_accuracy: Decimal | None
    outflow_accuracy: Decimal | None
    target_variance_pct: Decimal
    actual_variance_pct: Decimal | None
    within_target: bool | None
    weeks_measured: int


class EntityViewRow(BaseModel):
    week_number: int
    start_date: datetime.date
    end_date: datetime.date
    total_inflows: Decimal
    total_outflows: Decimal
    net_transfers: Decimal
    net_cash_flow: Decimal


class CurrencyViewRow(BaseModel):
    week_number: int
    start_date: datetime.date
    end_date: datetime.date
    opening_cash: Decimal
    total_inflows: Decimal
    total_outflows: Decimal
    net_transfers: Decimal
    net_cash_flow: Decimal
    closing_cash: Decimal


class WhatIfRequest(BaseModel):
    adjustments: list[ForecastAdjustmentCreate]
    label: str = "What-if scenario"


class WhatIfComparisonWeek(BaseModel):
    week_number: int
    base_closing_cash: Decimal
    scenario_closing_cash: Decimal
    closing_cash_difference: Decimal
    base_surplus_or_gap: Decimal
    scenario_surplus_or_gap: Decimal
    surplus_gap_difference: Decimal


class WhatIfResult(BaseModel):
    scenario_forecast_id: uuid.UUID
    weeks: list[WhatIfComparisonWeek]
