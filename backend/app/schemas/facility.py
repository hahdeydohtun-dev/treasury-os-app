import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.facility import (
    CollateralStatus,
    CollateralType,
    CommitmentType,
    CovenantOperator,
    CovenantStatus,
    CovenantType,
    DayCountConvention,
    DrawdownStatus,
    FacilityEventType,
    FacilityStatus,
    FeeStatus,
    FeeType,
    FundingActionStatus,
    FundingActionType,
    InterestRateType,
    RepaymentMethod,
    RepaymentStatus,
    RepaymentType,
)


class FacilityTypeOut(BaseModel):
    code: str
    name: str
    description: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class FacilityCreate(BaseModel):
    facility_reference: str
    facility_name: str
    facility_type_code: str
    commitment_type: CommitmentType
    lender_id: uuid.UUID
    legal_entity_id: uuid.UUID
    currency_code: str = Field(min_length=3, max_length=3)
    approved_limit: Decimal
    committed_limit: Decimal
    interest_rate_type: InterestRateType
    benchmark_rate: Decimal | None = None
    spread: Decimal | None = None
    fixed_rate: Decimal | None = None
    day_count_convention: DayCountConvention = DayCountConvention.ACT_365
    arrangement_fee: Decimal | None = None
    commitment_fee_rate: Decimal | None = None
    processing_fee: Decimal | None = None
    start_date: datetime.date
    availability_start_date: datetime.date | None = None
    availability_end_date: datetime.date | None = None
    maturity_date: datetime.date
    renewal_date: datetime.date | None = None
    repayment_frequency: str | None = None
    repayment_method: RepaymentMethod = RepaymentMethod.BULLET
    next_repayment_date: datetime.date | None = None
    purpose: str | None = None
    security_type: str | None = None
    collateral_description: str | None = None
    notice_period_days: int | None = None
    notes: str | None = None


class FacilityUpdate(BaseModel):
    committed_limit: Decimal | None = None
    fixed_rate: Decimal | None = None
    benchmark_rate: Decimal | None = None
    spread: Decimal | None = None
    maturity_date: datetime.date | None = None
    repayment_method: RepaymentMethod | None = None
    repayment_frequency: str | None = None
    change_reason: str


class FacilityOut(BaseModel):
    id: uuid.UUID
    facility_reference: str
    facility_name: str
    facility_type_code: str
    commitment_type: CommitmentType
    lender_id: uuid.UUID
    legal_entity_id: uuid.UUID
    currency_code: str
    approved_limit: Decimal
    committed_limit: Decimal
    current_drawn_amount: Decimal
    covenant_restricted_amount: Decimal
    interest_rate_type: InterestRateType
    benchmark_rate: Decimal | None
    spread: Decimal | None
    fixed_rate: Decimal | None
    day_count_convention: DayCountConvention
    arrangement_fee: Decimal | None
    commitment_fee_rate: Decimal | None
    processing_fee: Decimal | None
    start_date: datetime.date
    availability_start_date: datetime.date | None
    availability_end_date: datetime.date | None
    maturity_date: datetime.date
    renewal_date: datetime.date | None
    repayment_method: RepaymentMethod
    next_repayment_date: datetime.date | None
    status: FacilityStatus
    purpose: str | None
    version: int
    refinanced_from_facility_id: uuid.UUID | None
    is_active: bool

    model_config = {"from_attributes": True}


class FacilityUtilizationOut(BaseModel):
    committed_limit: Decimal
    drawn_amount: Decimal
    undrawn_amount: Decimal
    covenant_restricted_amount: Decimal
    available_amount: Decimal
    utilization_pct: Decimal
    effective_interest_rate: Decimal | None


class StatusChangeRequest(BaseModel):
    new_status: FacilityStatus
    reason: str | None = None


class FacilitySubLimitCreate(BaseModel):
    name: str
    purpose_code: str | None = None
    limit_amount: Decimal


class FacilitySubLimitOut(BaseModel):
    id: uuid.UUID
    facility_id: uuid.UUID
    name: str
    purpose_code: str | None
    limit_amount: Decimal
    drawn_amount: Decimal
    is_active: bool

    model_config = {"from_attributes": True}


class FacilityVersionOut(BaseModel):
    id: uuid.UUID
    facility_id: uuid.UUID
    version: int
    effective_date: datetime.date
    terms: dict
    change_reason: str | None

    model_config = {"from_attributes": True}


class DrawdownCreate(BaseModel):
    legal_entity_id: uuid.UUID
    sub_limit_id: uuid.UUID | None = None
    currency_code: str = Field(min_length=3, max_length=3)
    drawdown_amount: Decimal
    drawdown_date: datetime.date
    value_date: datetime.date | None = None
    maturity_date: datetime.date | None = None
    purpose: str | None = None
    reference: str | None = None


class DrawdownOut(BaseModel):
    id: uuid.UUID
    facility_id: uuid.UUID
    legal_entity_id: uuid.UUID
    sub_limit_id: uuid.UUID | None
    currency_code: str
    drawdown_amount: Decimal
    drawdown_date: datetime.date
    value_date: datetime.date | None
    maturity_date: datetime.date | None
    interest_rate_applicable: Decimal | None
    purpose: str | None
    reference: str | None
    status: DrawdownStatus

    model_config = {"from_attributes": True}


class RepaymentCreate(BaseModel):
    legal_entity_id: uuid.UUID
    drawdown_id: uuid.UUID | None = None
    currency_code: str = Field(min_length=3, max_length=3)
    repayment_type: RepaymentType
    original_amount: Decimal
    due_date: datetime.date
    is_early_repayment: bool = False
    reference: str | None = None


class RepaymentPaymentRequest(BaseModel):
    paid_amount: Decimal
    actual_payment_date: datetime.date


class RepaymentOut(BaseModel):
    id: uuid.UUID
    facility_id: uuid.UUID
    legal_entity_id: uuid.UUID
    currency_code: str
    repayment_type: RepaymentType
    original_amount: Decimal
    paid_amount: Decimal
    due_date: datetime.date
    actual_payment_date: datetime.date | None
    status: RepaymentStatus
    is_early_repayment: bool

    model_config = {"from_attributes": True}


class FeeCreate(BaseModel):
    legal_entity_id: uuid.UUID
    fee_type: FeeType
    currency_code: str = Field(min_length=3, max_length=3)
    amount: Decimal
    rate: Decimal | None = None
    calculation_basis: str | None = None
    due_date: datetime.date


class FeeOut(BaseModel):
    id: uuid.UUID
    facility_id: uuid.UUID
    legal_entity_id: uuid.UUID
    fee_type: FeeType
    currency_code: str
    amount: Decimal
    due_date: datetime.date
    paid_date: datetime.date | None
    status: FeeStatus

    model_config = {"from_attributes": True}


class CovenantCreate(BaseModel):
    name: str
    covenant_type: CovenantType
    description: str | None = None
    threshold: Decimal | None = None
    operator: CovenantOperator
    measurement_frequency: str | None = None
    warning_threshold: Decimal | None = None
    breach_threshold: Decimal | None = None
    cure_period_days: int | None = None
    notes: str | None = None


class CovenantUpdate(BaseModel):
    current_value: Decimal | None = None
    measurement_date: datetime.date | None = None


class CovenantOut(BaseModel):
    id: uuid.UUID
    facility_id: uuid.UUID
    name: str
    covenant_type: CovenantType
    threshold: Decimal | None
    operator: CovenantOperator
    current_value: Decimal | None
    headroom: Decimal | None
    status: CovenantStatus
    warning_threshold: Decimal | None
    breach_threshold: Decimal | None

    model_config = {"from_attributes": True}


class CollateralCreate(BaseModel):
    collateral_type: CollateralType
    description: str | None = None
    value: Decimal
    currency_code: str = Field(min_length=3, max_length=3)
    valuation_date: datetime.date
    haircut_pct: Decimal = Decimal(0)
    expiry_date: datetime.date | None = None
    document_reference: str | None = None


class CollateralOut(BaseModel):
    id: uuid.UUID
    facility_id: uuid.UUID
    collateral_type: CollateralType
    value: Decimal
    currency_code: str
    haircut_pct: Decimal
    eligible_value: Decimal
    status: CollateralStatus

    model_config = {"from_attributes": True}


class FacilityEventOut(BaseModel):
    id: uuid.UUID
    facility_id: uuid.UUID
    event_type: FacilityEventType
    event_date: datetime.datetime
    description: str
    related_record_type: str | None
    related_record_id: str | None

    model_config = {"from_attributes": True}


class FundingActionCreate(BaseModel):
    action_type: FundingActionType
    facility_id: uuid.UUID | None = None
    linked_drawdown_id: uuid.UUID | None = None
    linked_repayment_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID
    currency_code: str | None = None
    amount: Decimal | None = None
    proposed_date: datetime.date | None = None
    rationale: str | None = None
    payload: dict = {}


class FundingActionOut(BaseModel):
    id: uuid.UUID
    action_type: FundingActionType
    facility_id: uuid.UUID | None
    linked_drawdown_id: uuid.UUID | None
    linked_repayment_id: uuid.UUID | None
    legal_entity_id: uuid.UUID
    amount: Decimal | None
    status: FundingActionStatus
    rationale: str | None

    model_config = {"from_attributes": True}


class FundingCalendarEntry(BaseModel):
    date: datetime.date
    entity_id: uuid.UUID
    facility_id: uuid.UUID
    facility_name: str
    currency_code: str
    event_type: str
    amount: Decimal
    status: str


class FundingDashboardOut(BaseModel):
    total_committed_limit: Decimal
    total_drawn: Decimal
    total_undrawn: Decimal
    total_available: Decimal
    utilization_pct: Decimal
    facility_count: int
    by_currency: dict
    facilities_maturing_30_days: int
    facilities_maturing_90_days: int
    covenant_warnings: int
    covenant_breaches: int


class FundingGapOut(BaseModel):
    week_number: int
    projected_closing_cash: Decimal
    minimum_required_liquidity: Decimal
    cash_shortfall: Decimal
    committed_capacity_applied: Decimal
    remaining_unfunded_gap: Decimal


class FundingCapacityOut(BaseModel):
    committed_available: Decimal
    uncommitted_potential: Decimal
    total_potential_funding: Decimal
    facility_count: int
    by_currency: dict


class FundingCostReportRow(BaseModel):
    facility_id: uuid.UUID
    facility_name: str
    currency_code: str
    average_drawn: Decimal
    interest: Decimal
    fees: Decimal
    total_cost: Decimal
    effective_cost_pct: Decimal | None


class FundingRecommendationRow(BaseModel):
    facility_id: uuid.UUID
    facility_name: str
    currency_code: str
    available_amount: Decimal
    estimated_cost_pct: Decimal | None
    maturity_date: datetime.date
    commitment_type: CommitmentType
    eligible: bool
    ineligible_reasons: list
