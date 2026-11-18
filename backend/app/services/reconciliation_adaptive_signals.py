"""
Stage 5G: the signals an adaptive pattern is built from. Pure functions: no I/O, no randomness, no clock.

A "signal family" describes ONE independent aspect of how a bank row and a ledger row relate. The same function turns a pair
into signal keys whether the pair is a historical accepted/rejected decision (evidence) or a new candidate (scoring), so the
two are always comparable. A family is omitted when its inputs are missing; nothing is guessed.

No family is "amount similarity" or "description similarity" on its own: these describe RECURRING RELATIONSHIPS (a settlement
lag, a reference-format pairing, a payee pairing, a typical fee gap) and are only ever used together: a contribution needs at
least two independent families to be supported.
"""
import datetime
from dataclasses import dataclass
from decimal import Decimal

from app.services.reconciliation_normalization import normalize_narration, normalize_reference

FAMILIES = ("date_offset", "reference_format", "payee_pattern", "amount_relation")
FAMILY_LABELS = {
    "date_offset": "Settlement date offset", "reference_format": "Reference format pairing",
    "payee_pattern": "Recurring payee pattern", "amount_relation": "Typical amount difference",
}
_MAX_DATE_OFFSET = 10
_MAX_LABEL = 120


@dataclass(frozen=True)
class PairView:
    """The fields of one bank row and one ledger row that signals are derived from (never amounts of other entities)."""
    bank_date: datetime.date
    ledger_date: datetime.date
    bank_amount: Decimal
    ledger_amount: Decimal
    bank_references: tuple
    ledger_references: tuple
    bank_narration: str | None
    ledger_counterparty: str | None


def reference_shape(value: str | None) -> str | None:
    """'INV-00123' -> 'AAA-99999': letters and digits masked, separators and length kept."""
    v = (value or "").strip().upper()
    if not v:
        return None
    return "".join("9" if c.isdigit() else "A" if c.isalpha() else c for c in v[:40])


def _first(values) -> str | None:
    return next((v for v in values if v and str(v).strip()), None)


def reference_relation(bank_ref: str, ledger_ref: str) -> str:
    b, l = normalize_reference(bank_ref), normalize_reference(ledger_ref)
    if b == l:
        return "equal"
    if b in l or l in b:
        return "contains"
    return "different"


def narration_template(text: str | None) -> str | None:
    """The informative words of a bank narration: lowercase words of 3+ letters without digits, first four, in order."""
    tokens = [t for t in normalize_narration(text).split() if len(t) >= 3 and not any(c.isdigit() for c in t)]
    return " ".join(tokens[:4]) or None


def amount_relation(bank_amount: Decimal, ledger_amount: Decimal) -> str | None:
    diff = bank_amount - ledger_amount
    if diff == 0:
        return "exact"
    if ledger_amount == 0:
        return None
    pct = abs(diff) / abs(ledger_amount) * 100
    bucket = "<=0.1%" if pct <= Decimal("0.1") else "<=0.5%" if pct <= Decimal("0.5") else "<=1%" if pct <= 1 else ">1%"
    return f"{'bank_higher' if diff > 0 else 'bank_lower'}:{bucket}"


def pair_signals(pair: PairView) -> dict[str, str]:
    signals: dict[str, str] = {}
    offset = (pair.bank_date - pair.ledger_date).days
    if abs(offset) <= _MAX_DATE_OFFSET:
        signals["date_offset"] = f"{offset:+d}d"
    bank_ref, ledger_ref = _first(pair.bank_references), _first(pair.ledger_references)
    if bank_ref and ledger_ref:
        signals["reference_format"] = f"{reference_shape(bank_ref)}>{reference_shape(ledger_ref)}:{reference_relation(bank_ref, ledger_ref)}"
    template, payee = narration_template(pair.bank_narration), normalize_narration(pair.ledger_counterparty)
    if template and payee:
        signals["payee_pattern"] = f"{template}>{payee}"[:_MAX_LABEL]
    relation = amount_relation(pair.bank_amount, pair.ledger_amount)
    if relation:
        signals["amount_relation"] = relation
    return signals
