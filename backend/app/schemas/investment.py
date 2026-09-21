import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.investment import (
    DayCountConvention,
    InterestPaymentFrequency,
    InterestPaymentMethod,
    InvestmentRateType,
    InvestmentStatus,
    InvestmentTransactionStatus,
    InvestmentTransactionType,
    PenaltyType,
)


class InvestmentTypeOut(BaseModel):
    code: str
    name: str
    description: str | None
    is_implemented: bool
    is_active: bool

    model_config = {"from_attributes": True}


class InvestmentCreate(BaseModel):
    investment_reference: str
    investment_type_code: str
    legal_entity_id: uuid.UUID
    business_unit_id: uuid.UUID | None = None
    institution_id: uuid.UUID
    source_account_id: uuid.UUID | None = None
    destination_account_id: uuid.UUID | None = None
    currency_code: str = Field(min_length=3, max_length=3)
    principal_amount: Decimal
    start_date: datetime.date
    maturity_date: datetime.date
    interest_rate: Decimal
    rate_type: InvestmentRateType = InvestmentRateType.FIXED
    rate_source: str | None = None
    day_count_convention: DayCountConvention = DayCountConvention.ACT_365
    interest_payment_method: InterestPaymentMethod = InterestPaymentMethod.AT_MATURITY
    interest_payment_frequency: InterestPaymentFrequency | None = None
    early_termination_allowed: bool = True
    partial_termination_allowed: bool = True
    rollover_allowed: bool = True
    penalty_type: PenaltyType = PenaltyType.NONE
    penalty_rate: Decimal | None = None
    penalty_amount: Decimal | None = None
    purpose: str | None = None
    notes: str | None = None


class InvestmentUpdate(BaseModel):
    interest_rate: Decimal | None = None
    maturity_date: datetime.date | None = None
    change_reason: str


class InvestmentOut(BaseModel):
    id: uuid.UUID
    investment_reference: str
    investment_type_code: str
    legal_entity_id: uuid.UUID
    institution_id: uuid.UUID
    currency_code: str
    principal_amount: Decimal
    original_principal_amount: Decimal
    placement_date: datetime.date | None
    start_date: datetime.date
    maturity_date: datetime.date
    tenor_days: int
    interest_rate: Decimal
    rate_type: InvestmentRateType
    day_count_convention: DayCountConvention
    interest_payment_method: InterestPaymentMethod
    expected_interest: Decimal
    accrued_interest: Decimal
    received_interest: Decimal
    early_termination_allowed: bool
    partial_termination_allowed: bool
    rollover_allowed: bool
    status: InvestmentStatus
    version: int
    previous_investment_id: uuid.UUID | None
    rolled_to_investment_id: uuid.UUID | None
    is_active: bool

    model_config = {"from_attributes": True}


class StatusChangeRequest(BaseModel):
    new_status: InvestmentStatus
    reason: str | None = None


class PlacementRequest(BaseModel):
    placement_date: datetime.date
    value_date: datetime.date | None = None
    available_cash: Decimal | None = None


class TerminationRequest(BaseModel):
    amount: Decimal
    termination_date: datetime.date


class TerminationResultOut(BaseModel):
    principal_returned: Decimal
    interest_earned: Decimal
    interest_forfeited: Decimal
    penalty: Decimal
    net_proceeds: Decimal
    investment: InvestmentOut


class RolloverRequest(BaseModel):
    rollover_amount: Decimal
    new_rate: Decimal
    new_start_date: datetime.date
    new_maturity_date: datetime.date
    new_reference: str


class RebookingRequest(BaseModel):
    new_rate: Decimal | None = None
    new_maturity_date: datetime.date | None = None
    additional_principal: Decimal = Decimal(0)
    reason: str


class RolloverComparisonRequest(BaseModel):
    new_principal: Decimal
    new_rate: Decimal
    new_start_date: datetime.date
    new_maturity_date: datetime.date
    penalty: Decimal = Decimal(0)


class RolloverComparisonOut(BaseModel):
    current_rate: Decimal
    proposed_rate: Decimal
    current_maturity: datetime.date
    proposed_maturity: datetime.date
    principal: Decimal
    additional_principal: Decimal
    withdrawn_principal: Decimal
    expected_interest_current_term: Decimal
    expected_interest_new_term: Decimal
    penalty: Decimal
    net_expected_proceeds: Decimal


class InvestmentVersionOut(BaseModel):
    id: uuid.UUID
    investment_id: uuid.UUID
    version: int
    effective_date: datetime.date
    terms: dict
    change_reason: str | None

    model_config = {"from_attributes": True}


class InvestmentTransactionOut(BaseModel):
    id: uuid.UUID
    investment_id: uuid.UUID
    transaction_type: InvestmentTransactionType
    currency_code: str
    amount: Decimal
    transaction_date: datetime.date
    status: InvestmentTransactionStatus
    reference: str | None
    description: str | None
    related_investment_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class InvestmentEventOut(BaseModel):
    id: uuid.UUID
    investment_id: uuid.UUID
    event_type: str
    event_date: datetime.datetime
    description: str
    amount: Decimal | None
    currency_code: str | None

    model_config = {"from_attributes": True}


class InvestmentLiquidityOut(BaseModel):
    total_invested: Decimal
    maturing_7_days: Decimal
    maturing_30_days: Decimal
    maturing_90_days: Decimal
    total_expected_interest: Decimal
    weighted_average_rate: Decimal | None
    investment_count: int
    by_currency: dict
    by_entity: dict
    by_institution: dict


class ConcentrationRow(BaseModel):
    dimension: str
    key: str
    current_amount: Decimal
    limit_amount: Decimal | None
    available_capacity: Decimal | None
    utilization_pct: Decimal | None
    status: str


class InvestmentAlertOut(BaseModel):
    alert_type: str
    investment_id: uuid.UUID
    investment_reference: str
    message: str
    severity: str
    due_date: datetime.date | None
