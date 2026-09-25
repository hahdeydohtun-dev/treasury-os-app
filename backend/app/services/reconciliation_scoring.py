"""
Deterministic scoring model for Stage 5C matching (SECTIONS 19-25, 61).

Pure functions only - no I/O, no database access, no randomness, no
current-time dependency, no AI. Given the same two transaction records
and the same configuration, `score_candidate` always returns the exact
same result, satisfying SECTION 50's determinism requirement.

MATCHING_RULE_VERSION identifies this exact scoring model, candidate
rules, normalization rules, and thresholds together (SECTION 25) - a
future rule change must introduce a NEW version string, never silently
redefine what an existing stored version means, so a historical
suggestion's own `matching_rule_version` always tells a reviewer exactly
which rules produced it.

Weighting choice (SECTION 19): the old bank-matcher application
(forensic report, Section E) uses a continuous percentage-weighted
blend (amount 30% + reference 25% + date 20% + party 15% + side 10%,
each sub-score 0-100). That model is NOT reused verbatim here because it
cannot cleanly express SECTION 21's required EXACT-vs-TOLERANCE
distinction (an exact and a within-tolerance amount match blend into the
same continuous amount sub-score in the old model). Stage 5C instead
uses a fixed-point-bucket model - conceptually the same five signals,
weighted in a similar relative order (amount > reference > date >
direction > narration), but each signal contributes one of a small
number of DISCRETE point values specifically so "exact" and "tolerance"
are two clearly different, testable outcomes rather than two points on
a continuum. This is the one documented deviation from a straight port
of the old engine, and this paragraph is that documentation.
"""
import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from app.models.bank_statement import BankStatementEntryType
from app.models.lookup import CashDirection
from app.services.reconciliation_normalization import (
    normalize_narration,
    normalize_reference,
    tokenize_narration,
)

MATCHING_RULE_VERSION = "5C-1.0"

# --- Point buckets (SECTION 19) - centralized, never scattered magic numbers ---
AMOUNT_EXACT_POINTS = Decimal(40)
AMOUNT_TOLERANCE_POINTS = Decimal(20)
REFERENCE_EXACT_POINTS = Decimal(30)
REFERENCE_PARTIAL_POINTS = Decimal(15)
DATE_EXACT_POINTS = Decimal(15)
DATE_TOLERANCE_POINTS = Decimal(5)
DIRECTION_POINTS = Decimal(10)
NARRATION_POINTS = Decimal(5)
MAX_SCORE = Decimal(100)

# --- Thresholds (SECTION 24) - centralized, versioned alongside the rule version above ---
AUTO_MATCH_THRESHOLD = Decimal(70)   # >= this: a strong, single, unambiguous candidate
REVIEW_THRESHOLD = Decimal(40)       # >= this (but < AUTO_MATCH_THRESHOLD): a plausible candidate worth a suggestion
# Below REVIEW_THRESHOLD: not persisted as a candidate suggestion at all (SECTION 38's
# "eligible" concept - a candidate this weak was never really a plausible match).

# Narration-token-overlap ratio required for the narration signal to award any points
# (SECTION 14: "a narration similarity signal must never override a clearly incompatible
# amount/currency/entity/account" - this ratio is intentionally demanding, since narration
# is the lowest-weighted, least-reliable signal here).
_NARRATION_OVERLAP_RATIO = Decimal("0.6")


def calculate_amount_score(bank_amount: Decimal, ledger_amount: Decimal) -> tuple[Decimal, bool]:
    """
    Returns (points, is_exact). Candidate ELIGIBILITY (whether the amount
    is even within the configured tolerance at all) is enforced earlier,
    at the SQL candidate-generation stage (SECTION 11/16) - by the time a
    ledger transaction reaches this function, it has already passed that
    filter, so this function only distinguishes exact (diff == 0) from
    within-tolerance (diff > 0) for point allocation.
    """
    diff = abs(bank_amount - ledger_amount)
    if diff == 0:
        return AMOUNT_EXACT_POINTS, True
    return AMOUNT_TOLERANCE_POINTS, False


def calculate_date_score(bank_date: datetime.date, ledger_date: datetime.date) -> tuple[Decimal, bool]:
    """Returns (points, is_exact). Same eligibility-vs-scoring split as calculate_amount_score."""
    diff = abs((bank_date - ledger_date).days)
    if diff == 0:
        return DATE_EXACT_POINTS, True
    return DATE_TOLERANCE_POINTS, False


def calculate_reference_score(bank_references: list, ledger_references: list) -> Decimal:
    """
    SECTION 13: compares every available reference-like field on each
    side (bank_reference/external_transaction_id vs. reference/
    external_reference), after normalize_reference. An exact normalized
    match on ANY pair scores the full amount; a substring containment
    match (still fully deterministic - never a fuzzy/edit-distance
    comparison) scores partial credit; no usable reference on either
    side scores zero.
    """
    bank_normalized = {normalize_reference(r) for r in bank_references if r}
    ledger_normalized = {normalize_reference(r) for r in ledger_references if r}
    if not bank_normalized or not ledger_normalized:
        return Decimal(0)
    if bank_normalized & ledger_normalized:
        return REFERENCE_EXACT_POINTS
    for b in bank_normalized:
        for l in ledger_normalized:
            if b and l and (b in l or l in b):
                return REFERENCE_PARTIAL_POINTS
    return Decimal(0)


def calculate_direction_score(
    bank_entry_type: BankStatementEntryType, treasury_direction: CashDirection,
) -> Decimal | None:
    """
    SECTION 15: returns None when the directions are INCOMPATIBLE - the
    caller must treat None as an outright candidate rejection (never a
    mere zero-point contribution), since a bank CREDIT can never
    legitimately correspond to a ledger OUTFLOW and vice versa. A bank
    CREDIT (money arriving) is compatible only with a ledger INFLOW; a
    bank DEBIT (money leaving) is compatible only with a ledger OUTFLOW.
    """
    compatible = (
        (bank_entry_type == BankStatementEntryType.CREDIT and treasury_direction == CashDirection.INFLOW)
        or (bank_entry_type == BankStatementEntryType.DEBIT and treasury_direction == CashDirection.OUTFLOW)
    )
    return DIRECTION_POINTS if compatible else None


def calculate_narration_score(bank_narration: str | None, ledger_texts: list) -> Decimal:
    """
    SECTION 14: deterministic token-overlap only - no AI, no embeddings.
    Awards points only when a clear majority (SECTION-defined ratio) of
    the bank narration's own tokens are also present in one of the
    ledger side's texts (narration and/or counterparty) - a weak partial
    overlap contributes nothing, so this signal can never manufacture a
    match on its own for two otherwise-unrelated transactions.
    """
    bank_normalized = normalize_narration(bank_narration)
    bank_tokens = tokenize_narration(bank_normalized)
    if not bank_tokens:
        return Decimal(0)
    for ledger_text in ledger_texts:
        if not ledger_text:
            continue
        ledger_tokens = tokenize_narration(normalize_narration(ledger_text))
        if not ledger_tokens:
            continue
        overlap = len(bank_tokens & ledger_tokens)
        ratio = Decimal(overlap) / Decimal(len(bank_tokens))
        if ratio >= _NARRATION_OVERLAP_RATIO:
            return NARRATION_POINTS
    return Decimal(0)


@dataclass(frozen=True)
class CandidateScore:
    treasury_transaction_id: object
    total: Decimal
    amount_exact: bool
    date_exact: bool
    reference_points: Decimal
    narration_points: Decimal
    direction_points: Decimal | None
    reason_parts: list = field(default_factory=list)

    @property
    def is_exact_match(self) -> bool:
        """SECTION 21: EXACT means every scored signal that could be exact, is - not merely a high total."""
        return self.amount_exact and self.date_exact and self.reference_points == REFERENCE_EXACT_POINTS

    @property
    def reason(self) -> str:
        return "; ".join(self.reason_parts)


def score_candidate(
    bank_amount: Decimal, ledger_amount: Decimal, bank_date: datetime.date, ledger_date: datetime.date,
    bank_references: list, ledger_references: list, bank_entry_type: BankStatementEntryType,
    ledger_direction: CashDirection, bank_narration: str | None, ledger_texts: list,
    treasury_transaction_id,
) -> CandidateScore | None:
    """
    The single authoritative scoring function (SECTION 19: "ONE
    authoritative scoring definition"). Returns None if direction is
    incompatible (an outright rejection, never merely a low score -
    SECTION 15). Otherwise returns a CandidateScore with the total and a
    deterministic, human-readable reason (SECTION 32) built entirely from
    the sub-scores actually computed - never a templated "AI believes"
    phrase.
    """
    direction_points = calculate_direction_score(bank_entry_type, ledger_direction)
    if direction_points is None:
        return None

    amount_points, amount_exact = calculate_amount_score(bank_amount, ledger_amount)
    date_points, date_exact = calculate_date_score(bank_date, ledger_date)
    reference_points = calculate_reference_score(bank_references, ledger_references)
    narration_points = calculate_narration_score(bank_narration, ledger_texts)

    total = amount_points + date_points + reference_points + direction_points + narration_points

    reasons = []
    reasons.append("Exact amount" if amount_exact else "Amount within configured tolerance")
    reasons.append("exact transaction date" if date_exact else "transaction date within configured tolerance")
    if reference_points == REFERENCE_EXACT_POINTS:
        reasons.append("exact normalized reference match")
    elif reference_points == REFERENCE_PARTIAL_POINTS:
        reasons.append("partial deterministic reference overlap")
    else:
        reasons.append("no usable reference match")
    reasons.append("compatible debit/credit direction")
    if narration_points > 0:
        reasons.append("deterministic narration/party overlap")

    return CandidateScore(
        treasury_transaction_id=treasury_transaction_id, total=total, amount_exact=amount_exact,
        date_exact=date_exact, reference_points=reference_points, narration_points=narration_points,
        direction_points=direction_points, reason_parts=reasons,
    )
