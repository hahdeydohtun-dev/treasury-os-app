"""
Stage 5G: annotates a run's already-persisted Stage 5C suggestions with their adaptive explanation.

This runs AFTER Stage 5C/5D selection and BEFORE the Stage 5E open-item sync, and it is annotation only:
  * it never updates or deletes a suggestion (so `confidence`, `status`, `reason`, tier and decidability are exactly as 5C wrote
    them) and it never touches a proposal, decision, claim, open item or any financial record;
  * it writes one immutable `reconciliation_adaptive_scores` row per suggestion that does not have one yet, so re-running a
    run never recomputes history, and a later decision only ever influences runs that execute AFTER it;
  * it is fail-safe: it runs inside a savepoint, and any error rolls back only its own rows and leaves the run deterministic.
When adaptive matching is disabled (the default) it does nothing at all: no rows are written and any older explanations stay.
"""
import datetime
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import BankStatementTransaction
from app.models.reconciliation import (
    MatchSuggestionStatus,
    ReconciliationConfiguration,
    ReconciliationMatchSuggestion,
    ReconciliationRun,
)
from app.models.reconciliation_adaptive import ReconciliationAdaptiveScore
from app.models.treasury_transaction import TreasuryTransaction
from app.services.audit_service import record_audit_event
from app.services.reconciliation_adaptive_learning import aggregate, load_evidence, score_adaptively
from app.services.reconciliation_adaptive_settings import (
    ADAPTIVE_RULE_VERSION,
    ParsedSettings,
    parse_settings,
)
from app.services.reconciliation_adaptive_signals import PairView, pair_signals
from app.services.reconciliation_scoring import MATCHING_RULE_VERSION

logger = logging.getLogger(__name__)
MODULE = "BANK_RECONCILIATION"


@dataclass
class AnnotationResult:
    enabled: bool = False
    annotated: int = 0
    contributing: int = 0
    fallback: int = 0
    evidence_decisions: int = 0
    failed: bool = False
    error: str | None = None
    warnings: list = field(default_factory=list)


def settings_for(configuration: ReconciliationConfiguration | None) -> ParsedSettings:
    return parse_settings(configuration.matching_rule_config if configuration is not None else None)


async def _annotate(db: AsyncSession, run: ReconciliationRun, configuration, parsed: ParsedSettings,
                    cutoff: datetime.datetime) -> AnnotationResult:
    S, B, T, A = ReconciliationMatchSuggestion, BankStatementTransaction, TreasuryTransaction, ReconciliationAdaptiveScore
    settings = parsed.settings
    result = AnnotationResult(enabled=True, warnings=list(parsed.warnings))
    stmt = (select(S, B, T).join(B, B.id == S.bank_statement_transaction_id).join(T, T.id == S.treasury_transaction_id)
            .where(S.reconciliation_run_id == run.id, S.status == MatchSuggestionStatus.PENDING, S.confidence.is_not(None),
                   S.match_type.is_not(None), ~exists().where(A.suggestion_id == S.id)).order_by(S.created_at, S.id))
    rows = (await db.execute(stmt)).all()
    if not rows:
        return result
    evidence = await load_evidence(db, run.legal_entity_id, run.bank_account_id, cutoff, settings)
    stats = aggregate(evidence.rows, cutoff, settings)
    result.evidence_decisions = len(evidence.rows)
    snapshot = {"settings": settings.to_dict(), "warnings": parsed.warnings, "evidence_decisions_scanned": evidence.scanned,
                "evidence_decisions_eligible": len(evidence.rows), "evidence_truncated": evidence.truncated}
    for suggestion, bank, ledger in rows:
        if bank.currency_code != ledger.transaction_currency_code:
            continue
        signals = pair_signals(PairView(
            bank_date=bank.transaction_date, ledger_date=ledger.event_date, bank_amount=bank.amount, ledger_amount=ledger.transaction_amount,
            bank_references=(bank.bank_reference, bank.external_transaction_id), ledger_references=(ledger.reference, ledger.external_reference),
            bank_narration=bank.narration, ledger_counterparty=ledger.counterparty))
        outcome = score_adaptively(suggestion.confidence, bank.currency_code, bank.entry_type.value, suggestion.match_type.value,
                                   signals, stats, settings)
        db.add(ReconciliationAdaptiveScore(
            suggestion_id=suggestion.id, reconciliation_run_id=run.id, legal_entity_id=run.legal_entity_id, bank_account_id=run.bank_account_id,
            currency_code=bank.currency_code, direction=bank.entry_type.value, match_type=suggestion.match_type.value,
            deterministic_score=outcome.deterministic_score, adaptive_contribution=outcome.contribution,
            recommendation_score=outcome.recommendation, score_band=outcome.band, fallback=outcome.fallback,
            fallback_reason=outcome.fallback_reason, support_level=outcome.support_level,
            accepted_examples=outcome.accepted_examples, rejected_examples=outcome.rejected_examples,
            learning_rule_version=ADAPTIVE_RULE_VERSION, base_rule_version=suggestion.matching_rule_version or MATCHING_RULE_VERSION,
            configuration_id=configuration.id if configuration is not None else None,
            configuration_version=configuration.version if configuration is not None else None,
            evidence_cutoff=cutoff, settings_snapshot=snapshot, evidence=[p.to_json() for p in outcome.patterns]))
        result.annotated += 1
        result.fallback += 1 if outcome.fallback else 0
        result.contributing += 0 if outcome.fallback else 1
    await db.flush()
    return result


async def annotate_run_adaptively(
    db: AsyncSession, run: ReconciliationRun, configuration: ReconciliationConfiguration | None, user_id: uuid.UUID | None,
    *, evidence_cutoff: datetime.datetime | None = None,
) -> AnnotationResult:
    """Never raises: any failure is contained, audited, and leaves the run's deterministic results untouched."""
    parsed = settings_for(configuration)
    if not parsed.settings.enabled:
        return AnnotationResult(enabled=False, warnings=list(parsed.warnings))
    cutoff = evidence_cutoff or run.started_at or datetime.datetime.now(datetime.UTC)
    try:
        async with db.begin_nested():
            result = await _annotate(db, run, configuration, parsed, cutoff)
    except Exception as exc:
        logger.exception("adaptive annotation failed for run %s; deterministic results are unaffected", run.id)
        result = AnnotationResult(enabled=True, failed=True, error=type(exc).__name__)
    await record_audit_event(
        db, module=MODULE, action="ADAPTIVE_ANNOTATE_FAILED" if result.failed else "ADAPTIVE_ANNOTATE", record_type="ReconciliationRun",
        record_id=str(run.id), user_id=user_id, legal_entity_id=run.legal_entity_id,
        new_value={"learning_rule_version": ADAPTIVE_RULE_VERSION, "base_rule_version": MATCHING_RULE_VERSION,
                   "configuration_id": str(configuration.id) if configuration is not None else None,
                   "configuration_version": configuration.version if configuration is not None else None,
                   "evidence_cutoff": cutoff.isoformat(), "annotated": result.annotated, "contributing": result.contributing,
                   "fallback": result.fallback, "evidence_decisions": result.evidence_decisions, "error": result.error})
    return result


def explanation_text(row: ReconciliationAdaptiveScore) -> str:
    if row.fallback:
        return (f"Deterministic score {row.deterministic_score}. No adaptive adjustment was applied: {row.fallback_reason} "
                f"The recommendation equals the deterministic score. This is a suggestion only; a person must accept or reject it.")
    sign = "+" if row.adaptive_contribution >= 0 else ""
    return (f"Deterministic score {row.deterministic_score}. Patterns learned from {row.accepted_examples} accepted and "
            f"{row.rejected_examples} rejected earlier decisions on this account adjusted it by {sign}{row.adaptive_contribution} "
            f"(support {row.support_level}) to a recommendation of {row.recommendation_score}. "
            f"This is a suggestion only; a person must accept or reject it.")


def explanation_payload(row: ReconciliationAdaptiveScore, *, include_decision_ids: bool = False) -> dict:
    patterns = []
    for p in row.evidence:
        item = {k: v for k, v in p.items() if k != "decision_ids"}
        if include_decision_ids:
            item["decision_ids"] = p.get("decision_ids", [])
        patterns.append(item)
    return {
        "suggestion_id": str(row.suggestion_id), "reconciliation_run_id": str(row.reconciliation_run_id),
        "deterministic_score": str(row.deterministic_score), "adaptive_contribution": str(row.adaptive_contribution),
        "recommendation_score": str(row.recommendation_score), "score_band": row.score_band, "support_level": row.support_level,
        "fallback": row.fallback, "fallback_reason": row.fallback_reason, "accepted_examples": row.accepted_examples,
        "rejected_examples": row.rejected_examples, "learning_rule_version": row.learning_rule_version,
        "base_rule_version": row.base_rule_version, "configuration_version": row.configuration_version,
        "evidence_cutoff": row.evidence_cutoff.isoformat(),
        "scope": {"level": "account", "bank_account_id": str(row.bank_account_id), "currency_code": row.currency_code,
                  "direction": row.direction, "match_type": row.match_type},
        "patterns": patterns, "explanation": explanation_text(row), "human_review_required": True,
        "does_not_change": "The suggestion's status, deterministic confidence and whether it can be decided are unchanged.",
    }
