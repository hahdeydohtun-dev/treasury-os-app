"""
Reconciliation persistence foundation (Stage 5B).

Establishes the data model a future deterministic matching engine
(Stage 5C), advanced matching (Stage 5D), open-item workflow (Stage 5E),
and reports (Stage 5F) will all build on. This module deliberately
implements NO matching logic whatsoever - no candidate generation, no
scoring, no confidence calculation, no automatic matching decisions.
Every "matching-shaped" column here (confidence, match_type,
matching_rule_version, weights_version_id) is a reserved, currently-
unused placeholder Stage 5C+ will populate; Stage 5B never writes a
non-null value into any of them itself.

Architecture (SECTION 2/16 of the Stage 5B spec):

    BankStatementTransaction (Stage 5A - external bank evidence)
              |
              v
       ReconciliationRun (this stage - a scoped execution record)
              |
              v
    ReconciliationMatchSuggestion / ReconciliationOpenItem
    (this stage's tables - EMPTY until Stage 5C+ populates them)

No cash-ledger side effects: nothing in this module ever creates,
modifies, or deletes a TreasuryTransaction, BankBalance, or any
investment/facility financial record. Reconciliation is an analysis/
control layer that consumes evidence; it never mutates it.
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin, UUIDPKMixin


class ReconciliationRunStatus(str, Enum):
    """
    SECTION 5/14 of the Stage 5B spec: COMPLETED means the EXECUTION
    completed, never that "everything matched" - a completed run may
    legitimately contain zero matches and entirely open items. DRAFT is
    reserved for a future incremental-creation UX (see
    docs/STAGE_5B_RECONCILIATION_DATA_MODEL.md, "Design decisions") -
    Stage 5B's own create endpoint validates everything eagerly and
    creates runs directly into READY, never leaving one sitting in DRAFT.
    """
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# SECTION 4 (Stage 5B run-boundary addendum): an explicit transition
# table, the same "no arbitrary status change" discipline used
# everywhere else in this codebase (FACILITY_STATUS_TRANSITIONS,
# INVESTMENT_STATUS_TRANSITIONS).
RECONCILIATION_RUN_STATUS_TRANSITIONS: dict = {
    ReconciliationRunStatus.DRAFT: {ReconciliationRunStatus.READY, ReconciliationRunStatus.CANCELLED},
    ReconciliationRunStatus.READY: {ReconciliationRunStatus.RUNNING, ReconciliationRunStatus.CANCELLED},
    ReconciliationRunStatus.RUNNING: {ReconciliationRunStatus.COMPLETED, ReconciliationRunStatus.FAILED},
    ReconciliationRunStatus.COMPLETED: set(),
    ReconciliationRunStatus.FAILED: set(),
    ReconciliationRunStatus.CANCELLED: set(),
}


class ReconciliationRun(Base, UUIDPKMixin, TimestampMixin):
    """
    A scoped reconciliation job definition and execution record
    (SECTION 2 of the run-boundary addendum) - NOT itself a
    reconciliation result. Scoped strictly to one legal entity and one
    bank account (never group-wide - SECTION 6/11: "the execution layer
    must never broaden its query beyond the run's authorized scope").
    `group_id` is deliberately NOT stored redundantly alongside
    `legal_entity_id` - the group is always derivable via
    `legal_entity.group_id`, matching the established convention already
    used by every other entity-scoped model in this codebase (Investment,
    Facility, etc. never duplicate group_id either).
    """
    __tablename__ = "reconciliation_runs"

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False, index=True
    )
    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    status: Mapped[ReconciliationRunStatus] = mapped_column(
        default=ReconciliationRunStatus.READY, nullable=False, index=True
    )

    # SECTION 3.2/14/15 (Stage 5B run-boundary addendum): the EXACT
    # configuration version this run resolved at creation time - stored
    # so a completed run's result is always reproducible even if the
    # configuration is later superseded by a new version (mirrors
    # FXRate's own effective-dated versioning discipline).
    configuration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_configurations.id"), nullable=True
    )

    # SECTION 4/9 (Stage 5B spec): populated only by the execution
    # boundary, never by creation. Both default to 0 and remain 0 until
    # POST /reconciliation/runs/{id}/execute actually runs. Stage 5B
    # itself does no eligibility filtering - eligible_transaction_count
    # is set equal to statement_transaction_count for now, a placeholder
    # Stage 5C will refine once real matching eligibility rules exist.
    statement_transaction_count: Mapped[int] = mapped_column(default=0, nullable=False)
    eligible_transaction_count: Mapped[int] = mapped_column(default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    executed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MatchSuggestionStatus(str, Enum):
    """
    SECTION 9 of the Stage 5B spec: a suggestion is explicitly NOT the
    same thing as a final reconciliation result. PENDING is the only
    status Stage 5B itself could ever produce (and Stage 5B produces
    none at all, since no suggestions are created until Stage 5C exists)
    - ACCEPTED/REJECTED/SUPERSEDED are reserved for the future workflow
    that will actually decide a suggestion's fate.
    """
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class MatchType(str, Enum):
    """
    SECTION 7/8: a reserved, extensible vocabulary for the KIND of
    correspondence a future suggestion proposes - not itself a matching
    rule. OTHER is the only value any Stage 5B code could ever use if it
    needed a placeholder; in practice Stage 5B creates zero suggestions,
    so no code path in this stage sets any of these.
    """
    ONE_TO_ONE = "ONE_TO_ONE"
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_ONE = "MANY_TO_ONE"
    BATCH_PAYMENT = "BATCH_PAYMENT"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
    BANK_CHARGE = "BANK_CHARGE"
    FX_ADJUSTED = "FX_ADJUSTED"
    OTHER = "OTHER"


class ReconciliationMatchSuggestion(Base, UUIDPKMixin, TimestampMixin):
    """
    SECTION 7/9: persistence for a FUTURE matching engine's output -
    Stage 5B defines the shape only; no code in this stage ever inserts
    a row here. Both transaction references are nullable (SECTION 21:
    "do not make both sides mandatory") so a future "no candidate found"
    placeholder suggestion remains representable without inventing a
    fake counterpart record.
    """
    __tablename__ = "reconciliation_match_suggestions"

    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id"), nullable=False, index=True
    )
    bank_statement_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_statement_transactions.id"), nullable=True, index=True
    )
    treasury_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("treasury_transactions.id"), nullable=True, index=True
    )

    match_type: Mapped[MatchType | None] = mapped_column(nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    status: Mapped[MatchSuggestionStatus] = mapped_column(
        default=MatchSuggestionStatus.PENDING, nullable=False, index=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Reserved for Stage 5C (deterministic rule set version) and Stage
    # 5G (adaptive weights version) respectively - both nullable, no FK
    # on weights_version_id yet since no such table exists until Stage 5G.
    matching_rule_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    weights_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class OpenItemCategory(str, Enum):
    """SECTION 11: a controlled, extensible vocabulary for storage only - no
    classification logic exists in Stage 5B to assign any of these."""
    BANK_ONLY = "BANK_ONLY"
    LEDGER_ONLY = "LEDGER_ONLY"
    AMOUNT_VARIANCE = "AMOUNT_VARIANCE"
    TIMING_DIFFERENCE = "TIMING_DIFFERENCE"
    DUPLICATE = "DUPLICATE"
    BANK_CHARGE = "BANK_CHARGE"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
    FX_DIFFERENCE = "FX_DIFFERENCE"
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_ONE = "MANY_TO_ONE"
    BATCH_PAYMENT = "BATCH_PAYMENT"
    MISSING_LEDGER_ENTRY = "MISSING_LEDGER_ENTRY"
    MISSING_BANK_ENTRY = "MISSING_BANK_ENTRY"
    EXCEPTION = "EXCEPTION"


class OpenItemStatus(str, Enum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class ReconciliationOpenItem(Base, UUIDPKMixin, TimestampMixin):
    """
    SECTION 10/11/12: persistence for a FUTURE unresolved-item workflow
    (assignment/aging/resolution is Stage 5E's job) - Stage 5B defines
    the shape only; no code in this stage ever inserts a row here.
    `legal_entity_id`/`bank_account_id` are denormalized directly onto
    this table (not just reachable via the run) so a future RBAC-scoped
    list/aggregate query never needs to join through
    ReconciliationRun to enforce entity isolation - the same
    denormalization convention already used by InvestmentTransaction,
    FacilityEvent, etc.
    """
    __tablename__ = "reconciliation_open_items"

    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False, index=True
    )
    bank_statement_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_statement_transactions.id"), nullable=True, index=True
    )
    treasury_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("treasury_transactions.id"), nullable=True, index=True
    )

    category: Mapped[OpenItemCategory] = mapped_column(nullable=False, index=True)
    status: Mapped[OpenItemStatus] = mapped_column(default=OpenItemStatus.OPEN, nullable=False, index=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), ForeignKey("currencies.code"), nullable=False)

    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconciliationConfiguration(Base, UUIDPKMixin, TimestampMixin):
    """
    SECTION 13/14: versioned configuration STORAGE only - Stage 5C
    defines and consumes the actual deterministic matching rules; Stage
    5B never reads `matching_rule_config` to make any decision. Follows
    the exact effective-dated versioning discipline already established
    by `FXRate` (version / is_current / superseded_by_id) rather than a
    new versioning mechanism - a configuration change is always a NEW
    row, the prior row is superseded (not deleted or edited in place),
    so a `ReconciliationRun.configuration_id` FK always resolves to
    exactly the terms that were in effect when that run was created,
    forever.

    `legal_entity_id`/`bank_account_id`/`currency_code` are all nullable:
    null means "applies at the broader scope" (e.g. a null
    `bank_account_id` with a set `legal_entity_id` is an entity-wide
    default; all-null is the group/system-wide default). Which specific
    row is "the applicable effective configuration" for a given run is
    resolved by preferring the most specific non-null match, falling
    back progressively broader - see
    app/services/reconciliation_service.py::resolve_effective_configuration.
    """
    __tablename__ = "reconciliation_configurations"
    __table_args__ = (
        UniqueConstraint(
            "legal_entity_id", "bank_account_id", "currency_code", "version",
            name="uq_reconciliation_configuration_identity",
        ),
    )

    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=True, index=True
    )
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=True, index=True
    )
    currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)

    # Reserved configuration fields Stage 5C will read - Stage 5B stores
    # them, validates their basic shape, and does nothing else with them.
    amount_tolerance_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal(0), nullable=False)
    date_tolerance_days: Mapped[int] = mapped_column(default=0, nullable=False)
    high_value_threshold: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    duplicate_policy: Mapped[str | None] = mapped_column(String(50), nullable=True)
    matching_rule_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    is_current: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_configurations.id"), nullable=True
    )
    effective_from: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
