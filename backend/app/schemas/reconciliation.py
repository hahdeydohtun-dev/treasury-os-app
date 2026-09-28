import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel

from app.models.reconciliation import (
    MatchSuggestionStatus,
    MatchType,
    OpenItemCategory,
    OpenItemStatus,
    ReconciliationRunStatus,
)


class ReconciliationRunCreate(BaseModel):
    legal_entity_id: uuid.UUID
    bank_account_id: uuid.UUID
    period_start: datetime.date
    period_end: datetime.date
    configuration_id: uuid.UUID | None = None
    notes: str | None = None


class ReconciliationRunUpdate(BaseModel):
    notes: str | None = None


class ReconciliationRunOut(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    bank_account_id: uuid.UUID
    period_start: datetime.date
    period_end: datetime.date
    status: ReconciliationRunStatus
    configuration_id: uuid.UUID | None
    statement_transaction_count: int
    eligible_transaction_count: int
    candidate_count: int
    suggestion_count: int
    matched_count: int
    ambiguous_count: int
    unmatched_count: int
    matching_rule_version: str | None
    notes: str | None
    failure_reason: str | None
    started_at: datetime.datetime | None
    completed_at: datetime.datetime | None

    model_config = {"from_attributes": True}


class ReconciliationMatchSuggestionOut(BaseModel):
    id: uuid.UUID
    reconciliation_run_id: uuid.UUID
    bank_statement_transaction_id: uuid.UUID | None
    treasury_transaction_id: uuid.UUID | None
    match_type: MatchType | None
    confidence: Decimal | None
    status: MatchSuggestionStatus
    reason: str | None
    matching_rule_version: str | None

    model_config = {"from_attributes": True}


class ReconciliationOpenItemOut(BaseModel):
    id: uuid.UUID
    reconciliation_run_id: uuid.UUID
    legal_entity_id: uuid.UUID
    bank_account_id: uuid.UUID
    bank_statement_transaction_id: uuid.UUID | None
    treasury_transaction_id: uuid.UUID | None
    category: OpenItemCategory
    status: OpenItemStatus
    amount: Decimal
    currency_code: str
    assigned_to_user_id: uuid.UUID | None
    description: str | None
    reason: str | None
    resolved_at: datetime.datetime | None

    model_config = {"from_attributes": True}


class ReconciliationConfigurationCreate(BaseModel):
    legal_entity_id: uuid.UUID | None = None
    bank_account_id: uuid.UUID | None = None
    currency_code: str | None = None
    amount_tolerance_pct: Decimal = Decimal(0)
    date_tolerance_days: int = 0
    high_value_threshold: Decimal | None = None
    duplicate_policy: str | None = None
    matching_rule_config: dict = {}
    effective_from: datetime.date | None = None


class ReconciliationConfigurationOut(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID | None
    bank_account_id: uuid.UUID | None
    currency_code: str | None
    amount_tolerance_pct: Decimal
    date_tolerance_days: int
    high_value_threshold: Decimal | None
    duplicate_policy: str | None
    matching_rule_config: dict
    is_active: bool
    version: int
    is_current: bool
    superseded_by_id: uuid.UUID | None
    effective_from: datetime.date

    model_config = {"from_attributes": True}
