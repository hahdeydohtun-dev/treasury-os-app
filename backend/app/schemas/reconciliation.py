import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel

from app.models.reconciliation import (
    MatchGroupStatus,
    MatchRelationshipType,
    MatchSuggestionStatus,
    MatchType,
    OpenItemCategory,
    OpenItemEventType,
    OpenItemPriority,
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
    advanced_match_count: int
    ambiguous_advanced_count: int
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


class ReconciliationMatchGroupMemberOut(BaseModel):
    id: uuid.UUID
    bank_statement_transaction_id: uuid.UUID | None
    treasury_transaction_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class ReconciliationMatchGroupOut(BaseModel):
    id: uuid.UUID
    reconciliation_run_id: uuid.UUID
    relationship_type: MatchRelationshipType
    status: MatchGroupStatus
    bank_aggregate_amount: Decimal
    ledger_aggregate_amount: Decimal
    currency_code: str
    difference: Decimal
    fx_source_currency_code: str | None
    fx_target_currency_code: str | None
    fx_rate: Decimal | None
    fx_rate_date: datetime.date | None
    fx_rate_id: uuid.UUID | None
    fx_rate_type: str | None
    fx_rate_source: str | None
    fx_tolerance_pct: Decimal | None
    score: Decimal
    confidence: Decimal
    reason: str
    matching_rule_version: str
    members: list[ReconciliationMatchGroupMemberOut] = []

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
    priority: OpenItemPriority
    amount: Decimal
    currency_code: str
    item_date: datetime.date
    due_date: datetime.date | None
    description: str | None
    reason: str | None
    assigned_to_user_id: uuid.UUID | None
    assigned_by_user_id: uuid.UUID | None
    assigned_at: datetime.datetime | None
    resolved_at: datetime.datetime | None
    resolved_by_user_id: uuid.UUID | None
    resolution_reason: str | None
    closed_at: datetime.datetime | None
    closed_by_user_id: uuid.UUID | None
    rejected_at: datetime.datetime | None
    rejection_reason: str | None
    created_at: datetime.datetime
    # Days outstanding, computed at read time from item_date (never stored): up to today for an
    # actionable item, up to the resolution/closure/rejection date once it is no longer actionable.
    age_days: int | None = None

    model_config = {"from_attributes": True}


class OpenItemEventOut(BaseModel):
    id: uuid.UUID
    open_item_id: uuid.UUID
    event_type: OpenItemEventType
    actor_user_id: uuid.UUID | None
    from_status: OpenItemStatus | None
    to_status: OpenItemStatus | None
    assignee_user_id: uuid.UUID | None
    note: str | None
    reference: str | None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class OpenItemDetailOut(ReconciliationOpenItemOut):
    bank_transaction: dict | None = None
    ledger_transaction: dict | None = None
    suggestions: list[dict] = []
    match_groups: list[dict] = []


class OpenItemAssignRequest(BaseModel):
    assignee_user_id: uuid.UUID
    reason: str
    due_date: datetime.date | None = None
    priority: OpenItemPriority | None = None


class OpenItemReasonRequest(BaseModel):
    reason: str


class OpenItemNoteRequest(BaseModel):
    note: str | None = None


class OpenItemCommentRequest(BaseModel):
    note: str


class OpenItemEvidenceRequest(BaseModel):
    reference: str
    note: str | None = None


class OpenItemSyncOut(BaseModel):
    reconciliation_run_id: uuid.UUID
    created: int
    already_existing: int
    by_category: dict[str, int]


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
