"""
Stage 5G: learning evidence and adaptive scoring (v1: Stage 5C one-to-one suggestions; account-level learning only).

EVIDENCE comes straight from the authoritative Stage 5E decision records: `reconciliation_match_decisions` joined to the
suggestion it decided and the two transactions it referenced. Nothing is copied into a second dataset. A decision is eligible
evidence only if it passes EVERY check in `classify_decision` (an explicit ACCEPTED/REJECTED decision, internally consistent,
complete, in the same entity and bank account, both transactions still live, an accepted decision still has its claim, and
inside the evidence window and BEFORE the cutoff). Everything else is counted by reason, never silently used.

SCORING is deterministic and explainable: each signal family contributes at most its share of a strictly capped adjustment.
It never changes the suggestion, its confidence, its tier or its decidability: it is annotation, computed after selection.
All arithmetic is Decimal.
"""
import datetime
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import BankStatementTransaction, BankStatementTransactionStatus
from app.models.reconciliation import (
    MatchDecisionType,
    MatchProposalKind,
    ReconciliationAcceptedClaim,
    ReconciliationMatchDecision,
    ReconciliationMatchSuggestion,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.services.reconciliation_adaptive_settings import SUPPORT_LEVELS, AdaptiveSettings
from app.services.reconciliation_adaptive_signals import (
    FAMILIES,
    FAMILY_LABELS,
    PairView,
    pair_signals,
)
from app.services.reconciliation_scoring import AUTO_MATCH_THRESHOLD

MAX_DECISIONS_SCANNED = 5000
_ZERO = Decimal(0)
_K0 = Decimal(2)                        # prior strength: shrinks a pattern's balance toward zero when evidence is thin
_CENT = Decimal("0.01")
_REVIEW_CEILING = AUTO_MATCH_THRESHOLD - _CENT
FAMILY_SHARE = {"date_offset": Decimal("0.2"), "reference_format": Decimal("0.3"),
                "payee_pattern": Decimal("0.3"), "amount_relation": Decimal("0.2")}
EXCLUSION_REASONS = (
    "GROUP_PROPOSAL_NOT_LEARNED_IN_V1", "SUGGESTION_MISSING", "INVALID_DECISION_RECORD", "STATUS_MISMATCH", "INCOMPLETE_SUGGESTION",
    "RUN_MISMATCH", "RUN_NOT_COMPLETED", "TRANSACTION_MISSING", "SCOPE_MISMATCH", "CURRENCY_MISMATCH", "BANK_TRANSACTION_REVERSED",
    "LEDGER_TRANSACTION_NOT_POSTED", "ACCEPTED_WITHOUT_CLAIM", "OUTSIDE_EVIDENCE_WINDOW",
)


@dataclass(frozen=True)
class EvidenceRow:
    decision_id: uuid.UUID
    run_id: uuid.UUID
    decided_at: datetime.datetime
    accepted: bool
    currency: str
    direction: str
    match_type: str
    signals: dict


@dataclass
class LoadedEvidence:
    rows: list = field(default_factory=list)
    exclusions: dict = field(default_factory=dict)
    scanned: int = 0
    truncated: bool = False


def _fields() -> dict:
    D, R, S, B, T = (ReconciliationMatchDecision, ReconciliationRun, ReconciliationMatchSuggestion,
                     BankStatementTransaction, TreasuryTransaction)
    C = ReconciliationAcceptedClaim
    return {
        "d_id": D.id, "d_at": D.created_at, "d_decision": D.decision, "d_kind": D.kind, "d_new": D.new_status, "d_run": D.reconciliation_run_id,
        "r_status": R.status, "s_id": S.id, "s_status": S.status, "s_run": S.reconciliation_run_id, "s_bank": S.bank_statement_transaction_id,
        "s_ledger": S.treasury_transaction_id, "s_conf": S.confidence, "s_type": S.match_type,
        "b_entity": B.legal_entity_id, "b_account": B.bank_account_id, "b_status": B.status, "b_date": B.transaction_date, "b_amount": B.amount,
        "b_currency": B.currency_code, "b_entry": B.entry_type, "b_ref": B.bank_reference, "b_ext": B.external_transaction_id, "b_narr": B.narration,
        "t_entity": T.legal_entity_id, "t_account": T.bank_account_id, "t_status": T.status, "t_date": T.event_date, "t_amount": T.transaction_amount,
        "t_currency": T.transaction_currency_code, "t_ref": T.reference, "t_ext": T.external_reference, "t_party": T.counterparty,
        "has_claim": exists().where(C.decision_id == D.id),
    }


def classify_decision(m: Any, entity_id: uuid.UUID, account_id: uuid.UUID, cutoff: datetime.datetime, max_age_days: int) -> str | None:
    """None if the decision is eligible learning evidence, else the first exclusion reason. `m` is a row mapping."""
    if m["d_kind"] != MatchProposalKind.SUGGESTION:
        return "GROUP_PROPOSAL_NOT_LEARNED_IN_V1"
    if m["s_id"] is None:
        return "SUGGESTION_MISSING"
    if m["d_decision"] not in (MatchDecisionType.ACCEPTED, MatchDecisionType.REJECTED) or m["d_new"] != m["d_decision"].value:
        return "INVALID_DECISION_RECORD"
    if m["s_status"].value != m["d_decision"].value:
        return "STATUS_MISMATCH"
    if None in (m["s_bank"], m["s_ledger"], m["s_conf"], m["s_type"]):
        return "INCOMPLETE_SUGGESTION"
    if m["s_run"] != m["d_run"]:
        return "RUN_MISMATCH"
    if m["r_status"] != ReconciliationRunStatus.COMPLETED:
        return "RUN_NOT_COMPLETED"
    if m["b_entity"] is None or m["t_entity"] is None:
        return "TRANSACTION_MISSING"
    if m["b_entity"] != entity_id or m["t_entity"] != entity_id or m["b_account"] != account_id or m["t_account"] != account_id:
        return "SCOPE_MISMATCH"
    if m["b_currency"] != m["t_currency"]:
        return "CURRENCY_MISMATCH"
    if m["b_status"] != BankStatementTransactionStatus.ACTIVE:
        return "BANK_TRANSACTION_REVERSED"
    if m["t_status"] != TransactionStatus.POSTED:
        return "LEDGER_TRANSACTION_NOT_POSTED"
    if m["d_decision"] == MatchDecisionType.ACCEPTED and not m["has_claim"]:
        return "ACCEPTED_WITHOUT_CLAIM"
    if (cutoff - m["d_at"]).days > max_age_days:
        return "OUTSIDE_EVIDENCE_WINDOW"
    return None


async def load_evidence(db: AsyncSession, entity_id: uuid.UUID, account_id: uuid.UUID, cutoff: datetime.datetime,
                        settings: AdaptiveSettings) -> LoadedEvidence:
    """All decisions of THIS entity and bank account decided strictly before `cutoff`, classified. Nothing outside the scope is read."""
    D, R, S, B, T = (ReconciliationMatchDecision, ReconciliationRun, ReconciliationMatchSuggestion,
                     BankStatementTransaction, TreasuryTransaction)
    cols = _fields()
    stmt = (select(*[c.label(k) for k, c in cols.items()]).select_from(D)
            .join(R, R.id == D.reconciliation_run_id).outerjoin(S, S.id == D.suggestion_id)
            .outerjoin(B, B.id == S.bank_statement_transaction_id).outerjoin(T, T.id == S.treasury_transaction_id)
            .where(D.legal_entity_id == entity_id, R.legal_entity_id == entity_id, R.bank_account_id == account_id, D.created_at < cutoff)
            .order_by(D.created_at.desc(), D.id).limit(MAX_DECISIONS_SCANNED + 1))
    raw = (await db.execute(stmt)).all()
    out = LoadedEvidence(exclusions={r: 0 for r in EXCLUSION_REASONS})
    out.truncated = len(raw) > MAX_DECISIONS_SCANNED
    for row in raw[:MAX_DECISIONS_SCANNED]:
        m = row._mapping
        out.scanned += 1
        reason = classify_decision(m, entity_id, account_id, cutoff, settings.max_evidence_age_days)
        if reason is not None:
            out.exclusions[reason] += 1
            continue
        pair = PairView(
            bank_date=m["b_date"], ledger_date=m["t_date"], bank_amount=m["b_amount"], ledger_amount=m["t_amount"],
            bank_references=(m["b_ref"], m["b_ext"]), ledger_references=(m["t_ref"], m["t_ext"]),
            bank_narration=m["b_narr"], ledger_counterparty=m["t_party"])
        out.rows.append(EvidenceRow(
            decision_id=m["d_id"], run_id=m["d_run"], decided_at=m["d_at"], accepted=m["d_decision"] == MatchDecisionType.ACCEPTED,
            currency=m["b_currency"], direction=m["b_entry"].value, match_type=m["s_type"].value, signals=pair_signals(pair)))
    return out


# --------------------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------------------
def decay_weight(age_days: int, half_life_days: int) -> Decimal:
    """Halves for every FULL half-life of age (a step function: exact, no floating point)."""
    return Decimal(1) / (Decimal(2) ** (max(age_days, 0) // half_life_days))


@dataclass
class PatternStat:
    accepted: int = 0
    rejected: int = 0
    aw: Decimal = _ZERO
    rw: Decimal = _ZERO
    runs: set = field(default_factory=set)
    decisions: list = field(default_factory=list)       # (decision_id, accepted, weight)


def aggregate(rows: list, cutoff: datetime.datetime, settings: AdaptiveSettings) -> dict:
    """{(currency, direction, match_type, family, key): PatternStat}. Evidence is only ever combined within one such scope."""
    stats: dict = {}
    for e in rows:
        weight = decay_weight((cutoff - e.decided_at).days, settings.half_life_days)
        for family, key in e.signals.items():
            s = stats.setdefault((e.currency, e.direction, e.match_type, family, key), PatternStat())
            if e.accepted:
                s.accepted += 1
                s.aw += weight
            else:
                s.rejected += 1
                s.rw += weight
            s.runs.add(e.run_id)
            s.decisions.append((e.decision_id, e.accepted, weight))
    return stats


# --------------------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------------------
def _balance(aw: Decimal, rw: Decimal) -> Decimal:
    return (aw - rw) / (aw + rw + _K0)


def evaluate_pattern(stat: PatternStat, settings: AdaptiveSettings) -> tuple[bool, Decimal, str | None]:
    """(supported, balance, why-not). Rejections only ever pull the balance down; they are never positive evidence."""
    n = stat.accepted + stat.rejected
    balance = _balance(stat.aw, stat.rw)
    if n < settings.min_evidence:
        return False, balance, f"only {n} earlier decision(s); at least {settings.min_evidence} are required"
    if len(stat.runs) < settings.min_distinct_runs:
        return False, balance, f"the decisions come from {len(stat.runs)} run(s); at least {settings.min_distinct_runs} are required"
    if balance == 0:
        return False, balance, "accepted and rejected evidence is evenly split"
    for _decision_id, accepted, weight in stat.decisions:         # leave-one-out: no single decision may decide the direction
        without = _balance(stat.aw - (weight if accepted else _ZERO), stat.rw - (_ZERO if accepted else weight))
        if without == 0 or (without > 0) != (balance > 0):
            return False, balance, "the direction of the evidence depends on a single decision"
    return True, balance, None


@dataclass(frozen=True)
class PatternEvidence:
    family: str
    label: str
    accepted: int
    rejected: int
    balance: Decimal
    contribution: Decimal
    supported: bool
    note: str | None
    decision_ids: tuple

    def to_json(self) -> dict:
        return {"family": self.family, "family_label": FAMILY_LABELS[self.family], "label": self.label, "accepted": self.accepted,
                "rejected": self.rejected, "balance": str(self.balance.quantize(Decimal("0.001"), ROUND_HALF_UP)),
                "contribution": str(self.contribution.quantize(_CENT, ROUND_HALF_UP)), "supported": self.supported, "note": self.note,
                "decision_ids": [str(d) for d in self.decision_ids]}


@dataclass(frozen=True)
class AdaptiveOutcome:
    deterministic_score: Decimal
    contribution: Decimal
    recommendation: Decimal
    band: str
    support_level: str
    fallback: bool
    fallback_reason: str | None
    accepted_examples: int
    rejected_examples: int
    patterns: list


def score_band(deterministic: Decimal) -> str:
    return "AUTO" if deterministic >= AUTO_MATCH_THRESHOLD else "REVIEW"


def tier_locked_recommendation(deterministic: Decimal, adaptive: Decimal) -> tuple[Decimal, str]:
    """
    The recommendation is the deterministic score plus the adaptive adjustment, locked inside the deterministic score's own
    tier: a suggestion at or above the auto-match threshold stays at or above it, and one below it stays strictly below it
    (69.99 at most). The displayed score can therefore never suggest more (or less) actionability than the suggestion has.
    """
    band = score_band(deterministic)
    raw = deterministic + adaptive
    low, high = (AUTO_MATCH_THRESHOLD, Decimal(100)) if band == "AUTO" else (_ZERO, _REVIEW_CEILING)
    return min(max(raw, low), high).quantize(_CENT, ROUND_HALF_UP), band


def _support_level(supported: list, settings: AdaptiveSettings) -> str:
    if not supported:
        return "NONE"
    families = len(supported)
    min_n = min(p[1] for p in supported)
    mean_abs = sum((abs(p[2]) for p in supported), _ZERO) / families
    if families >= 3 and min_n >= 2 * settings.min_evidence and mean_abs >= Decimal("0.5"):
        return "HIGH"
    if families >= 3 or (families >= 2 and min_n >= 2 * settings.min_evidence):
        return "MEDIUM"
    return "LOW"


def score_adaptively(
    deterministic: Decimal, currency: str, direction: str, match_type: str, signals: dict, stats: dict, settings: AdaptiveSettings,
) -> AdaptiveOutcome:
    considered: list = []          # (family, key, stat, supported, balance, why)
    for family in FAMILIES:
        key = signals.get(family)
        stat = stats.get((currency, direction, match_type, family, key)) if key is not None else None
        if stat is None:
            continue
        supported, balance, why = evaluate_pattern(stat, settings)
        considered.append((family, key, stat, supported, balance, why))
    supported_rows = [c for c in considered if c[3]]
    level = _support_level([(c[0], c[2].accepted + c[2].rejected, c[4]) for c in supported_rows], settings)

    fallback_reason = None
    if not supported_rows:
        fallback_reason = (f"No learned pattern for this account, currency, direction and match type has enough evidence "
                           f"(at least {settings.min_evidence} decisions from {settings.min_distinct_runs} runs, with a stable direction).")
    elif len(supported_rows) < settings.min_signal_families:
        fallback_reason = (f"Only {len(supported_rows)} independent signal family has enough evidence; "
                           f"{settings.min_signal_families} are required before adaptive evidence is applied.")
    elif SUPPORT_LEVELS.index(level) < SUPPORT_LEVELS.index(settings.min_support_level):
        fallback_reason = f"Support level {level} is below the configured minimum {settings.min_support_level}."

    contributions: dict = {}
    total = _ZERO
    if fallback_reason is None:
        for family, _key, _stat, _supported, balance, _why in supported_rows:
            cap = settings.max_boost if balance > 0 else settings.max_penalty
            contributions[family] = FAMILY_SHARE[family] * cap * balance
            total += contributions[family]
        total = min(max(total, -settings.max_penalty), settings.max_boost).quantize(_CENT, ROUND_HALF_UP)

    patterns = [PatternEvidence(
        family=family, label=key, accepted=stat.accepted, rejected=stat.rejected, balance=balance,
        contribution=contributions.get(family, _ZERO), supported=supported, note=why,
        decision_ids=tuple(d for d, _a, _w in sorted(stat.decisions, key=lambda x: str(x[0]))[:50]) if supported else (),
    ) for family, key, stat, supported, balance, why in considered]

    used = supported_rows if fallback_reason is None else []
    used_decisions = {d: a for c in used for d, a, _w in c[2].decisions}
    recommendation, band = tier_locked_recommendation(deterministic, total)
    return AdaptiveOutcome(
        deterministic_score=deterministic, contribution=total, recommendation=recommendation, band=band, support_level=level,
        fallback=fallback_reason is not None, fallback_reason=fallback_reason,
        accepted_examples=sum(1 for a in used_decisions.values() if a), rejected_examples=sum(1 for a in used_decisions.values() if not a),
        patterns=patterns,
    )
