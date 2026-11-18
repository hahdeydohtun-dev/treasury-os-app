"""
Stage 5G: persisted explanation of an adaptive recommendation for ONE Stage 5C one-to-one suggestion.

One immutable row per scored suggestion. It records the original deterministic score, the capped adaptive contribution, the
tier-locked recommendation score, whether the engine fell back to deterministic scoring and why, the rule and configuration
versions, the evidence cutoff and a summary of the learned patterns used. It deliberately holds NO transaction details: only
pattern labels, counts and the ids of the original decisions (traceability), all from the SAME entity and bank account.

The suggestion row itself is never touched: `confidence`, `status`, `reason` and decidability stay exactly as Stage 5C wrote them.
Rows are append-only (a database trigger refuses UPDATE and DELETE), and a second trigger refuses an INSERT whose entity,
bank account or run does not match the suggestion's own run, so a row can never be attributed to the wrong scope.
"""
import datetime
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, UUIDPKMixin


class ReconciliationAdaptiveScore(Base, UUIDPKMixin):
    __tablename__ = "reconciliation_adaptive_scores"
    __table_args__ = (
        UniqueConstraint("suggestion_id", name="uq_reconciliation_adaptive_score_suggestion"),
        CheckConstraint("deterministic_score >= 0 AND deterministic_score <= 100", name="ck_adaptive_score_deterministic_range"),
        CheckConstraint("recommendation_score >= 0 AND recommendation_score <= 100", name="ck_adaptive_score_recommendation_range"),
        CheckConstraint("adaptive_contribution >= -15 AND adaptive_contribution <= 15", name="ck_adaptive_score_contribution_ceiling"),
        CheckConstraint("score_band IN ('AUTO', 'REVIEW')", name="ck_adaptive_score_band"),
        CheckConstraint("support_level IN ('NONE', 'LOW', 'MEDIUM', 'HIGH')", name="ck_adaptive_score_support_level"),
        CheckConstraint("accepted_examples >= 0 AND rejected_examples >= 0", name="ck_adaptive_score_example_counts"),
        CheckConstraint(
            "NOT fallback OR (adaptive_contribution = 0 AND recommendation_score = deterministic_score AND fallback_reason IS NOT NULL)",
            name="ck_adaptive_score_fallback_is_deterministic",
        ),
        Index("ix_adaptive_scores_scope", "legal_entity_id", "bank_account_id"),
    )

    suggestion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reconciliation_match_suggestions.id"), nullable=False)
    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id"), nullable=False, index=True)
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False)
    bank_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False)
    # the evidence scope this score was computed in (account-level learning only)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)         # bank entry type: CREDIT / DEBIT
    match_type: Mapped[str] = mapped_column(String(30), nullable=False)        # EXACT / TOLERANCE

    deterministic_score: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)      # == the suggestion's own confidence
    adaptive_contribution: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)    # signed, capped; 0 on fallback
    recommendation_score: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)     # tier-locked
    score_band: Mapped[str] = mapped_column(String(10), nullable=False)
    fallback: Mapped[bool] = mapped_column(nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    support_level: Mapped[str] = mapped_column(String(10), nullable=False)
    accepted_examples: Mapped[int] = mapped_column(nullable=False)
    rejected_examples: Mapped[int] = mapped_column(nullable=False)

    learning_rule_version: Mapped[str] = mapped_column(String(20), nullable=False)
    base_rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    configuration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_configurations.id"), nullable=True)
    configuration_version: Mapped[int | None] = mapped_column(nullable=True)
    evidence_cutoff: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settings_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
