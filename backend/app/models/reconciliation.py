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

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    # POST /reconciliation/runs/{id}/execute actually runs.
    statement_transaction_count: Mapped[int] = mapped_column(default=0, nullable=False)
    eligible_transaction_count: Mapped[int] = mapped_column(default=0, nullable=False)

    # SECTION 47 (Stage 5C): deterministic matching-engine execution
    # summary counts, populated only by execute_reconciliation_run.
    # `matched_count` = suggestions with an unambiguous single top
    # candidate meeting AUTO_MATCH_THRESHOLD; `ambiguous_count` = bank
    # transactions with 2+ candidates tied at the top score (SECTION 23/
    # 68 - never arbitrarily resolved); `unmatched_count` = bank
    # transactions with no candidate clearing REVIEW_THRESHOLD.
    # `candidate_count`/`suggestion_count` are raw totals across the
    # whole run. "Matched" here means "the deterministic engine
    # proposed a suggestion" - SECTION 48 is explicit that this is NOT
    # the same as "reconciled" (a human/workflow outcome, not built yet).
    candidate_count: Mapped[int] = mapped_column(default=0, nullable=False)
    suggestion_count: Mapped[int] = mapped_column(default=0, nullable=False)
    matched_count: Mapped[int] = mapped_column(default=0, nullable=False)
    ambiguous_count: Mapped[int] = mapped_column(default=0, nullable=False)
    unmatched_count: Mapped[int] = mapped_column(default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SECTION 25 (Stage 5C): the exact matching-rule version this run's
    # execution used - stored on the run itself (in addition to being
    # stored per-suggestion) so a run's own summary counts can always be
    # traced back to precisely which scoring/threshold rules produced
    # them, even before opening any individual suggestion.
    matching_rule_version: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # SECTION 26 (Stage 5D): advanced-match run summary counters, additive
    # and backward-compatible - existing Stage 5C counts
    # (statement_transaction_count/eligible_transaction_count/
    # candidate_count/suggestion_count/matched_count/ambiguous_count/
    # unmatched_count) keep their exact prior meaning unchanged.
    # `matched_count` IS Stage 5C's own one-to-one match count (no
    # separate "one_to_one_match_count" column was added - reusing the
    # existing name per the task's own "or use the repository's existing
    # naming conventions" allowance). `advanced_match_count` counts
    # Stage 5D match GROUPS (ONE_TO_MANY/MANY_TO_ONE/BATCH/
    # INTERNAL_TRANSFER/FX_MATCH) with status PENDING (i.e. proposed,
    # unambiguous); `ambiguous_advanced_count` counts bank/ledger
    # transactions for which Stage 5D found 2+ competing advanced
    # candidate groupings and deliberately made no automatic selection
    # (SECTION 18).
    advanced_match_count: Mapped[int] = mapped_column(default=0, nullable=False)
    ambiguous_advanced_count: Mapped[int] = mapped_column(default=0, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    executed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MatchRelationshipType(str, Enum):
    """
    SECTION 6/7 (Stage 5D): the cardinality/relationship a
    ReconciliationMatchGroup proposes. Deliberately excludes an
    unrestricted MANY_TO_MANY value - SECTION 7 is explicit that Stage
    5D never performs unrestricted many-to-many matching; a situation
    that would require it surfaces as AMBIGUOUS competing groups instead
    (MatchGroupStatus.AMBIGUOUS), never as a new relationship type.
    """
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_ONE = "MANY_TO_ONE"
    BATCH = "BATCH"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
    FX_MATCH = "FX_MATCH"


class MatchGroupStatus(str, Enum):
    """
    SECTION 18/24: PENDING is the only status Stage 5D itself ever sets
    for a genuinely proposed, unambiguous group. AMBIGUOUS marks 2+
    competing groups where no automatic selection was made - Stage 5D
    persists every competing candidate as its own AMBIGUOUS row rather
    than picking one. ACCEPTED/REJECTED are reserved for a future
    workflow stage (Stage 5E) - no Stage 5D code path ever sets them.
    """
    PENDING = "PENDING"
    AMBIGUOUS = "AMBIGUOUS"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ReconciliationMatchGroup(Base, UUIDPKMixin, TimestampMixin):
    """
    SECTION 6 (Stage 5D): a proposed grouped relationship between one or
    more BankStatementTransaction rows and one or more TreasuryTransaction
    rows - member rows live in `ReconciliationMatchGroupMember`, never
    inline here, so the group itself stays a fixed-shape record
    regardless of how many members it has. A group NEVER creates,
    modifies, or deletes any TreasuryTransaction/BankStatementTransaction
    - it only describes a relationship among EXISTING evidence (SECTION
    5). `group_key` backs the idempotency guarantee (SECTION 21): a
    deterministic string built from
    (relationship_type, sorted bank member IDs, sorted ledger member IDs)
    - see reconciliation_advanced_matching.py::compute_group_key - with a
    database-level unique constraint on (reconciliation_run_id,
    group_key), so re-running the same run's advanced-matching pass can
    never persist the same group twice.
    """
    __tablename__ = "reconciliation_match_groups"
    __table_args__ = (
        UniqueConstraint("reconciliation_run_id", "group_key", name="uq_reconciliation_match_group_identity"),
    )

    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id"), nullable=False, index=True
    )
    relationship_type: Mapped[MatchRelationshipType] = mapped_column(nullable=False, index=True)
    status: Mapped[MatchGroupStatus] = mapped_column(default=MatchGroupStatus.PENDING, nullable=False, index=True)

    bank_aggregate_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    ledger_aggregate_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    difference: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)

    # FX evidence (SECTION 16) - all nullable, populated only for
    # relationship_type == FX_MATCH; every other relationship type
    # leaves these null (same currency on both sides, no conversion).
    # SECTION 16A.8 (FX rate selection hardening): a reference to the
    # authoritative FXRate row is stored (`fx_rate_id`), not merely its
    # numeric value, so the conversion remains reproducible even if that
    # rate is later superseded (SECTION 16A.9) - `fx_rate`/`fx_rate_date`
    # are still stored directly too, as a denormalized convenience/
    # historical snapshot, but `fx_rate_id` is the authoritative
    # provenance link. `fx_rate_type`/`fx_rate_source` record which rate
    # TYPE and SOURCE were actually used, and `fx_tolerance_pct` records
    # the tolerance actually applied, so the whole conversion decision is
    # reconstructable from this row alone.
    fx_source_currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    fx_target_currency_code: Mapped[str | None] = mapped_column(String(3), nullable=True)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    fx_rate_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    fx_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("fx_rates.id"), nullable=True)
    fx_rate_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fx_rate_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fx_tolerance_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)

    score: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    matching_rule_version: Mapped[str] = mapped_column(String(50), nullable=False)

    group_key: Mapped[str] = mapped_column(String(255), nullable=False)

    members: Mapped[list["ReconciliationMatchGroupMember"]] = relationship(
        back_populates="match_group", cascade="all, delete-orphan",
    )


class ReconciliationMatchGroupMember(Base, UUIDPKMixin, TimestampMixin):
    """
    One member of a ReconciliationMatchGroup - exactly one of
    `bank_statement_transaction_id`/`treasury_transaction_id` is set
    (enforced by `ck_reconciliation_match_group_member_exactly_one_side`),
    never both and never neither, so a member row is unambiguously
    "a bank-side participant" or "a ledger-side participant." A group's
    full membership is the set of its member rows - source records are
    never merged, copied, or destroyed (SECTION 12 of the Stage 5D spec:
    "never merge or destroy the source records," carried over from the
    original forensic report's own one-to-many/many-to-one design).
    """
    __tablename__ = "reconciliation_match_group_members"
    __table_args__ = (
        CheckConstraint(
            "(bank_statement_transaction_id IS NOT NULL)::int + (treasury_transaction_id IS NOT NULL)::int = 1",
            name="ck_reconciliation_match_group_member_exactly_one_side",
        ),
    )

    match_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_match_groups.id"), nullable=False, index=True
    )
    bank_statement_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_statement_transactions.id"), nullable=True, index=True
    )
    treasury_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("treasury_transactions.id"), nullable=True, index=True
    )

    match_group: Mapped["ReconciliationMatchGroup"] = relationship(back_populates="members")


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
    rule. EXACT and TOLERANCE (Stage 5C) distinguish whether every
    exactly-comparable signal (amount, date, reference) matched exactly
    or only within the run's configured tolerance - see
    docs/STAGE_5C_DETERMINISTIC_MATCHING_ENGINE.md. The remaining values
    are reserved for later stages: ONE_TO_MANY/MANY_TO_ONE/BATCH_PAYMENT/
    INTERNAL_TRANSFER/FX_ADJUSTED (Stage 5D), BANK_CHARGE (Stage 5D), and
    OTHER as a general fallback. Stage 5C is one-to-one matching only
    (SECTION 23 of the Stage 5C spec) - it never sets ONE_TO_ONE or any
    of the Stage 5D-reserved values itself.
    """
    EXACT = "EXACT"
    TOLERANCE = "TOLERANCE"
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

    Stage 5C idempotency (SECTIONS 27): two partial unique indexes
    (hand-written in their own migration, not declared here - the same
    established convention as every other partial unique index in this
    codebase) back the application-level check in
    reconciliation_matching_engine.py: at most one suggestion per
    (run, bank_statement_transaction_id, treasury_transaction_id) when
    treasury_transaction_id is set, and at most one "no candidate found"
    suggestion (treasury_transaction_id IS NULL) per
    (run, bank_statement_transaction_id).
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
