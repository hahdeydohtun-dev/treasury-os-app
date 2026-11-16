"""
Stage 5F: reconciliation reports (read-only).

Every report is a pure read over the Stage 5A-5E tables. Nothing here creates, updates or deletes a bank-statement
transaction, ledger transaction, balance, proposal, decision, claim, open item or event; there is no second source of
truth and no caching, so a report is always reproducible from the source rows by the rules below.

COUNTING RULES (the same everywhere; see docs/STAGE_5F_RECONCILIATION_REPORTS.md)

* A *transaction* (bank statement row or ledger row), a *proposal* (a Stage 5C suggestion or a Stage 5D group), an
  *accepted match* (a proposal with an explicit accept decision) and an *exception* (an open item) are four different
  things and are never added together.
* Population: eligible bank rows are ACTIVE statement rows, eligible ledger rows are POSTED ledger rows, in the
  entity/account and date window of the report. The two populations are disjoint tables, so their counts may be added.
* Each eligible transaction falls into EXACTLY ONE bucket, so a transaction is never counted twice however many
  proposals or groups mention it:
    accepted   - it has an accepted claim (bank: MATCH; ledger: MATCH or TRANSFER)
    proposed   - not accepted, and a PENDING proposal (suggestion or group) covers it
    unmatched  - neither
  accepted + proposed + unmatched == eligible, per side, always.
* Proposals are counted separately from transactions (decision/group counts vs unique transaction counts) and the
  per-type figures are NOT additive: one ledger row can be in a one-to-one match and an internal-transfer pairing.
* Completion rate = (accepted bank + accepted ledger) / (eligible bank + eligible ledger), as a percentage; undefined
  (None) when there are no eligible transactions. It uses unique transactions and accepted claims, never proposals.
* Exceptions are open items. Account/entity level counts use distinct `source_key` so the same underlying exception
  seen by two runs is one exception.
* Money is returned as strings, per currency; amounts in different currencies are never added, and no FX conversion
  is performed. Percentages are the only rounded figures (HALF_UP, 2 dp) and are ratios, not money.
* Dates and ageing use UTC. Age in days = (as-of date) - (UTC date the item was opened); the as-of date defaults to
  today (UTC). Historical status and assignee are reconstructed from the Stage 5E event history, never inferred from
  the item's current status.
"""
from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import (
    Date,
    Integer,
    String,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    type_coerce,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    apply_resolved_entity_scope,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.models.bank_statement import BankStatementTransaction, BankStatementTransactionStatus
from app.models.banking import Bank, BankAccount
from app.models.entity import LegalEntity
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.models.reconciliation import (
    AcceptedClaimKind,
    MatchGroupStatus,
    MatchProposalKind,
    MatchSuggestionStatus,
    OpenItemCategory,
    OpenItemEventType,
    OpenItemStatus,
    ReconciliationAcceptedClaim,
    ReconciliationMatchDecision,
    ReconciliationMatchGroup,
    ReconciliationMatchGroupMember,
    ReconciliationMatchSuggestion,
    ReconciliationOpenItem,
    ReconciliationOpenItemEvent,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.schemas.banking import BankAccountOut

MODULE = TreasuryModule.BANK_RECONCILIATION
ACTIONABLE = (OpenItemStatus.OPEN, OpenItemStatus.UNDER_REVIEW)
FINAL = (OpenItemStatus.RESOLVED, OpenItemStatus.CLOSED, OpenItemStatus.REJECTED)
#: Event types that count as investigation activity (an assignment alone is not investigation).
ACTIVITY_EVENTS = (
    OpenItemEventType.INVESTIGATION_STARTED, OpenItemEventType.COMMENT, OpenItemEventType.EVIDENCE,
    OpenItemEventType.RESOLVED, OpenItemEventType.CLOSED, OpenItemEventType.REJECTED, OpenItemEventType.REOPENED,
)
#: (key, lower, upper-inclusive or None, label) - the default ageing bands.
AGEING_BANDS = [
    ("0-7", 0, 7, "0-7 days"), ("8-14", 8, 14, "8-14 days"), ("15-30", 15, 30, "15-30 days"),
    ("31-60", 31, 60, "31-60 days"), ("60+", 61, None, "More than 60 days"),
]
DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 366
MAX_EXPORT_ROWS = 50_000
_ZERO = Decimal(0)


# ======================================================================================
# formatting helpers
# ======================================================================================
def money(value: Any) -> str:
    """Exact 2-dp string. Refuses (rather than rounds) anything the ledger could not have stored."""
    d = Decimal(value if value is not None else 0)
    q = d.quantize(Decimal("0.01"))
    if q != d:
        raise ValueError(f"amount {d} has more than 2 decimal places")
    return format(q, "f")


def pct(numerator: int, denominator: int) -> str | None:
    if not denominator:
        return None
    return format((Decimal(numerator) * 100 / Decimal(denominator)).quantize(Decimal("0.01"), ROUND_HALF_UP), "f")


def short_ref(prefix: str, value: uuid.UUID) -> str:
    return f"{prefix}-{value.hex[:8].upper()}"


def utc_date(moment: datetime.datetime) -> datetime.date:
    return moment.astimezone(datetime.UTC).date()


def currency_text(amounts: dict[str, Decimal]) -> str:
    return "; ".join(f"{cur} {Decimal(v):,.2f}" for cur, v in sorted(amounts.items())) or "-"


def currency_rows(amounts: dict[str, Decimal]) -> list[dict[str, str]]:
    return [{"currency": cur, "amount": money(v)} for cur, v in sorted(amounts.items())]


def add_amount(target: dict[str, Decimal], currency: str, value: Any) -> None:
    target[currency] = target.get(currency, _ZERO) + Decimal(value or 0)


def band_for(age_days: int) -> str:
    for key, low, high, _label in AGEING_BANDS:
        if age_days >= low and (high is None or age_days <= high):
            return key
    return AGEING_BANDS[0][0]


# ======================================================================================
# filters and scope
# ======================================================================================
@dataclass
class ReportFilters:
    date_from: datetime.date
    date_to: datetime.date
    as_of: datetime.date
    period_explicit: bool = False
    legal_entity_id: uuid.UUID | None = None
    bank_account_id: uuid.UUID | None = None
    currency_code: str | None = None
    run_status: list[ReconciliationRunStatus] | None = None
    exception_status: list[OpenItemStatus] | None = None
    category: list[OpenItemCategory] | None = None
    assigned_to_user_id: uuid.UUID | None = None
    unassigned: bool = False
    txn_date_from: datetime.date | None = None
    txn_date_to: datetime.date | None = None
    min_age_days: int | None = None
    max_age_days: int | None = None
    page: int = 1
    page_size: int = 25
    paginate: bool = True

    def describe(self) -> dict[str, Any]:
        """The applied filters, for the report header, exports and the audit trail (no confidential data)."""
        raw = {
            "date_from": self.date_from, "date_to": self.date_to, "as_of": self.as_of,
            "legal_entity_id": self.legal_entity_id, "bank_account_id": self.bank_account_id,
            "currency_code": self.currency_code,
            "run_status": [s.value for s in self.run_status] if self.run_status else None,
            "exception_status": [s.value for s in self.exception_status] if self.exception_status else None,
            "category": [c.value for c in self.category] if self.category else None,
            "assigned_to_user_id": self.assigned_to_user_id, "unassigned": self.unassigned or None,
            "txn_date_from": self.txn_date_from, "txn_date_to": self.txn_date_to,
            "min_age_days": self.min_age_days, "max_age_days": self.max_age_days,
        }
        return {k: str(v) if isinstance(v, (datetime.date, uuid.UUID)) else v for k, v in raw.items() if v is not None}


def build_filters(
    *, date_from=None, date_to=None, as_of=None, today: datetime.date | None = None, **kwargs: Any,
) -> ReportFilters:
    """Validate filter combinations. Raises HTTP 422 with a specific message; never silently widens a request."""
    today = today or datetime.datetime.now(datetime.UTC).date()
    explicit = date_from is not None or date_to is not None
    if date_to is None:
        date_to = today if date_from is None else max(date_from, today)
    if date_from is None:
        date_from = date_to - datetime.timedelta(days=DEFAULT_PERIOD_DAYS - 1)
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to.")
    if (date_to - date_from).days + 1 > MAX_PERIOD_DAYS:
        raise HTTPException(status_code=422, detail=f"The reporting period may not exceed {MAX_PERIOD_DAYS} days.")
    tf, tt = kwargs.get("txn_date_from"), kwargs.get("txn_date_to")
    if tf and tt and tf > tt:
        raise HTTPException(status_code=422, detail="txn_date_from must not be after txn_date_to.")
    lo, hi = kwargs.get("min_age_days"), kwargs.get("max_age_days")
    if lo is not None and hi is not None and lo > hi:
        raise HTTPException(status_code=422, detail="min_age_days must not exceed max_age_days.")
    if as_of is not None and as_of > today:
        raise HTTPException(status_code=422, detail="as_of must not be in the future.")
    cur = kwargs.get("currency_code")
    if cur is not None:
        cur = cur.strip().upper()
        if len(cur) != 3 or not cur.isalpha():
            raise HTTPException(status_code=422, detail="currency_code must be a 3-letter code.")
        kwargs["currency_code"] = cur
    if kwargs.get("unassigned") and kwargs.get("assigned_to_user_id"):
        raise HTTPException(status_code=422, detail="unassigned and assigned_to_user_id cannot be combined.")
    return ReportFilters(date_from=date_from, date_to=date_to, as_of=as_of or today, period_explicit=explicit, **kwargs)


@dataclass
class ReportScope:
    """The entities the caller may see, derived on the server from their permissions - never from request input."""
    entity_ids: Any  # "ALL" or a set of UUIDs

    def apply(self, stmt, column):
        return apply_resolved_entity_scope(stmt, column, self.entity_ids)

    def allows(self, entity_id: uuid.UUID) -> bool:
        return self.entity_ids == "ALL" or entity_id in self.entity_ids


def _intersect(a, b):
    if a == "ALL":
        return b
    if b == "ALL":
        return a
    return set(a) & set(b)


async def build_scope(db: AsyncSession, user: User, filters: ReportFilters, *, export: bool = False) -> ReportScope:
    """VIEW scope for screens; for exports the INTERSECTION of VIEW and EXPORT scope, so an export can never be
    broader than what the user can see. A requested entity/account outside that scope is refused, never trusted."""
    view = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if view.is_empty:
        raise HTTPException(status_code=403, detail="No BANK_RECONCILIATION:VIEW permission.")
    ids = await resolve_scope_entity_ids(db, view)
    if export:
        exp = await get_authorized_scope(db, user, MODULE, TreasuryAction.EXPORT)
        if exp.is_empty:
            raise HTTPException(status_code=403, detail="No BANK_RECONCILIATION:EXPORT permission.")
        ids = _intersect(ids, await resolve_scope_entity_ids(db, exp))
    scope = ReportScope(ids)
    if filters.legal_entity_id is not None and not scope.allows(filters.legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")
    if filters.bank_account_id is not None:
        account = await db.get(BankAccount, filters.bank_account_id)
        if account is None or not scope.allows(account.legal_entity_id):
            raise HTTPException(status_code=404, detail="Bank account not found.")
        if filters.legal_entity_id is not None and account.legal_entity_id != filters.legal_entity_id:
            raise HTTPException(status_code=422, detail="bank_account_id does not belong to legal_entity_id.")
    return scope


# ======================================================================================
# result structure shared by screens and exports
# ======================================================================================
def col(key: str, label: str, align: str = "left") -> dict[str, str]:
    return {"key": key, "label": label, "align": align}


def kpi(key: str, label: str, value: Any, definition: str, source: str, drill: dict | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "definition": definition, "source": source, "drilldown": drill}


def table(key: str, title: str, columns: list[dict], rows: list[dict], *, total_rows: int | None = None,
          page: int | None = None, page_size: int | None = None, note: str | None = None) -> dict[str, Any]:
    return {"key": key, "title": title, "columns": columns, "rows": rows,
            "total_rows": len(rows) if total_rows is None else total_rows, "page": page, "page_size": page_size, "note": note}


async def entity_rows(db: AsyncSession, scope: ReportScope, filters: ReportFilters) -> list[LegalEntity]:
    stmt = scope.apply(select(LegalEntity), LegalEntity.id).order_by(LegalEntity.name)
    if filters.legal_entity_id:
        stmt = stmt.where(LegalEntity.id == filters.legal_entity_id)
    return list((await db.execute(stmt)).scalars().all())


async def new_result(
    db: AsyncSession, scope: ReportScope, filters: ReportFilters, *, report: str, title: str,
    definitions: list[str], group_level: bool = False, period_based: bool = True,
) -> dict[str, Any]:
    entities = await entity_rows(db, scope, filters)
    names = [f"{e.name} ({e.code})" for e in entities]
    account_scope = "All authorized bank accounts"
    if filters.bank_account_id:
        acct = await db.get(BankAccount, filters.bank_account_id)
        account_scope = f"Bank account {BankAccountOut.mask_account_number(acct.account_number)}" if acct else account_scope
    level = "GROUP" if group_level else ("ENTITY" if len(entities) == 1 else "MULTI-ENTITY (authorized entities only)")
    return {
        "report": report, "title": title, "generated_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "level": level,
        "reporting_period": {"from": str(filters.date_from), "to": str(filters.date_to),
                             "applies": period_based or filters.period_explicit},
        "as_of": str(filters.as_of), "timezone": "UTC",
        "scope": {"entities": names, "accounts": account_scope, "currency": filters.currency_code or "All currencies (shown separately)"},
        "filters": filters.describe(), "kpis": [], "tables": [], "notes": [], "definitions": definitions,
    }


# ======================================================================================
# accounts and populations
# ======================================================================================
async def load_accounts(db: AsyncSession, scope: ReportScope, filters: ReportFilters) -> list[dict[str, Any]]:
    stmt = (select(BankAccount, Bank.name, LegalEntity.name, LegalEntity.code, LegalEntity.group_id)
            .join(Bank, Bank.id == BankAccount.bank_id).join(LegalEntity, LegalEntity.id == BankAccount.legal_entity_id)
            .where(BankAccount.is_active.is_(True)))
    stmt = scope.apply(stmt, BankAccount.legal_entity_id)
    if filters.legal_entity_id:
        stmt = stmt.where(BankAccount.legal_entity_id == filters.legal_entity_id)
    if filters.bank_account_id:
        stmt = stmt.where(BankAccount.id == filters.bank_account_id)
    if filters.currency_code:
        stmt = stmt.where(BankAccount.currency_code == filters.currency_code)
    stmt = stmt.order_by(LegalEntity.name, BankAccount.account_name, BankAccount.id)
    return [{"id": a.id, "entity_id": a.legal_entity_id, "entity_name": en, "entity_code": ec, "group_id": gid,
             "bank_name": bn, "account_name": a.account_name, "currency": a.currency_code,
             "masked": BankAccountOut.mask_account_number(a.account_number)}
            for a, bn, en, ec, gid in (await db.execute(stmt)).all()]


def _empty_side() -> dict[str, int]:
    return {"total": 0, "accepted": 0, "proposed": 0, "unmatched": 0}


async def population(
    db: AsyncSession, scope: ReportScope, account_ids: list[uuid.UUID], date_from: datetime.date,
    date_to: datetime.date, currency: str | None,
) -> dict[uuid.UUID, dict[str, Any]]:
    """Per account: bank and ledger bucket counts (accepted / proposed / unmatched) and per-currency amounts."""
    out: dict[uuid.UUID, dict[str, Any]] = {a: {"bank": _empty_side(), "ledger": _empty_side(), "amounts": {}} for a in account_ids}
    if not account_ids:
        return out
    S, G, M, C = ReconciliationMatchSuggestion, ReconciliationMatchGroup, ReconciliationMatchGroupMember, ReconciliationAcceptedClaim

    def pending_bank(col):
        return or_(
            exists().where(S.bank_statement_transaction_id == col, S.treasury_transaction_id.is_not(None), S.status == MatchSuggestionStatus.PENDING),
            exists().where(M.bank_statement_transaction_id == col, M.match_group_id == G.id, G.status == MatchGroupStatus.PENDING))

    def pending_ledger(col):
        return or_(
            exists().where(S.treasury_transaction_id == col, S.status == MatchSuggestionStatus.PENDING),
            exists().where(M.treasury_transaction_id == col, M.match_group_id == G.id, G.status == MatchGroupStatus.PENDING))

    B, T = BankStatementTransaction, TreasuryTransaction
    for side, model, acct_col, cur_col, amt_col, date_col, status_clause, accepted, proposed in (
        ("bank", B, B.bank_account_id, B.currency_code, B.amount, B.transaction_date, B.status == BankStatementTransactionStatus.ACTIVE,
         exists().where(C.bank_statement_transaction_id == B.id, C.claim_kind == AcceptedClaimKind.MATCH), pending_bank(B.id)),
        ("ledger", T, T.bank_account_id, T.transaction_currency_code, T.transaction_amount, T.event_date, T.status == TransactionStatus.POSTED,
         exists().where(C.treasury_transaction_id == T.id), pending_ledger(T.id)),
    ):
        prop_only = and_(~accepted, proposed)
        stmt = (select(acct_col, cur_col, func.count(), func.count().filter(accepted), func.count().filter(prop_only),
                       func.coalesce(func.sum(amt_col), 0), func.coalesce(func.sum(amt_col).filter(accepted), 0),
                       func.coalesce(func.sum(amt_col).filter(prop_only), 0))
                .where(status_clause, acct_col.in_(account_ids), date_col >= date_from, date_col <= date_to)
                .group_by(acct_col, cur_col))
        stmt = scope.apply(stmt, model.legal_entity_id)
        if currency:
            stmt = stmt.where(cur_col == currency)
        for acct, cur, total, acc, prop, s_total, s_acc, s_prop in (await db.execute(stmt)).all():
            entry = out[acct]
            for k, v in (("total", total), ("accepted", acc), ("proposed", prop), ("unmatched", total - acc - prop)):
                entry[side][k] += v
            amounts = entry["amounts"].setdefault(cur, {})
            for k, v in (("accepted", s_acc), ("proposed", s_prop), ("unmatched", Decimal(s_total) - Decimal(s_acc) - Decimal(s_prop)), ("total", s_total)):
                amounts[f"{side}_{k}"] = amounts.get(f"{side}_{k}", _ZERO) + Decimal(v)
    return out


def side_sum(pop: dict[str, Any]) -> dict[str, int]:
    return {k: pop["bank"][k] + pop["ledger"][k] for k in ("total", "accepted", "proposed", "unmatched")}


def merge_amounts(target: dict[str, dict[str, Decimal]], source: dict[str, dict[str, Decimal]]) -> None:
    for cur, parts in source.items():
        bucket = target.setdefault(cur, {})
        for k, v in parts.items():
            bucket[k] = bucket.get(k, _ZERO) + v


def _exception_base(scope: ReportScope, filters: ReportFilters, *, date_window: bool, assignee_filter: bool = True):
    """Open items visible to the caller, filtered. `date_window` applies the reporting period to the item's
    transaction date (used by period-based reports); backlog reports use only their explicit filters."""
    I = ReconciliationOpenItem
    stmt = scope.apply(select(I), I.legal_entity_id)
    if filters.legal_entity_id:
        stmt = stmt.where(I.legal_entity_id == filters.legal_entity_id)
    if filters.bank_account_id:
        stmt = stmt.where(I.bank_account_id == filters.bank_account_id)
    if filters.currency_code:
        stmt = stmt.where(I.currency_code == filters.currency_code)
    if filters.category:
        stmt = stmt.where(I.category.in_(filters.category))
    if assignee_filter and filters.assigned_to_user_id:
        stmt = stmt.where(I.assigned_to_user_id == filters.assigned_to_user_id)
    if assignee_filter and filters.unassigned:
        stmt = stmt.where(I.assigned_to_user_id.is_(None))
    if date_window:
        stmt = stmt.where(I.item_date >= filters.date_from, I.item_date <= filters.date_to)
    if filters.txn_date_from:
        stmt = stmt.where(I.item_date >= filters.txn_date_from)
    if filters.txn_date_to:
        stmt = stmt.where(I.item_date <= filters.txn_date_to)
    return stmt


async def account_exception_stats(db, scope, filters, account_ids) -> dict[uuid.UUID, dict[str, Any]]:
    """Outstanding (OPEN/UNDER_REVIEW) exceptions per account on transactions dated in the period; unique by source_key."""
    I = ReconciliationOpenItem
    base = _exception_base(scope, filters, date_window=True).where(I.status.in_(ACTIONABLE), I.bank_account_id.in_(account_ids)).subquery()
    stmt = (select(base.c.bank_account_id, func.count(func.distinct(base.c.source_key)),
                   func.count(func.distinct(base.c.source_key)).filter(base.c.assigned_to_user_id.is_(None)),
                   func.min(base.c.created_at), base.c.currency_code, func.sum(base.c.amount))
            .group_by(base.c.bank_account_id, base.c.currency_code))
    # the distinct counts must not be split by currency: take them from a currency-free pass
    counts = (select(base.c.bank_account_id, func.count(func.distinct(base.c.source_key)),
                     func.count(func.distinct(base.c.source_key)).filter(base.c.assigned_to_user_id.is_(None)),
                     func.min(base.c.created_at)).group_by(base.c.bank_account_id))
    out: dict[uuid.UUID, dict[str, Any]] = {a: {"outstanding": 0, "unassigned": 0, "oldest_opened": None, "amounts": {}} for a in account_ids}
    for acct, n, un, oldest in (await db.execute(counts)).all():
        out[acct].update(outstanding=n, unassigned=un, oldest_opened=oldest)
    for acct, _n, _u, _o, cur, total in (await db.execute(stmt)).all():
        add_amount(out[acct]["amounts"], cur, total)
    return out


async def account_run_stats(db, scope, filters, account_ids) -> dict[uuid.UUID, dict[str, Any]]:
    R = ReconciliationRun
    base = scope.apply(select(R).where(R.bank_account_id.in_(account_ids)), R.legal_entity_id)
    runs = list((await db.execute(base.order_by(R.created_at.desc(), R.id))).scalars().all())
    out: dict[uuid.UUID, dict[str, Any]] = {a: {"latest": None, "latest_completed_at": None, "completed_in_period": 0} for a in account_ids}
    for run in runs:
        entry = out[run.bank_account_id]
        if entry["latest"] is None:
            entry["latest"] = run
        if run.status == ReconciliationRunStatus.COMPLETED:
            if run.completed_at and (entry["latest_completed_at"] is None or run.completed_at > entry["latest_completed_at"]):
                entry["latest_completed_at"] = run.completed_at
            if run.period_start <= filters.date_to and run.period_end >= filters.date_from:
                entry["completed_in_period"] += 1
    return out


# ======================================================================================
# 3.1 reconciliation run summary
# ======================================================================================
async def run_summary_report(db, scope, filters) -> dict[str, Any]:
    R = ReconciliationRun
    result = await new_result(db, scope, filters, report="run-summary", title="Reconciliation Run Summary", definitions=[
        "A run reconciles one bank account over one period. Eligible bank rows are ACTIVE statement rows; eligible ledger rows are POSTED ledger rows, both inside the run's entity, account and period.",
        "Accepted = the transaction has an accepted claim. Proposed = not accepted but covered by a PENDING proposal. Unmatched = neither. The three always add up to the eligible count.",
        "Pending proposals are proposals awaiting an explicit decision (the match-review queue); they are NOT counted as completed reconciliation. Rejected proposals are explicit rejections.",
        "Exceptions are open items, counted by their current status; they are a different thing from transactions and proposals and are not added to them.",
        "Runs shown are those whose reconciliation period overlaps the reporting period.",
    ])
    stmt = (scope.apply(select(R, BankAccount, LegalEntity.name).join(BankAccount, BankAccount.id == R.bank_account_id)
                        .join(LegalEntity, LegalEntity.id == R.legal_entity_id), R.legal_entity_id)
            .where(R.period_start <= filters.date_to, R.period_end >= filters.date_from))
    if filters.legal_entity_id:
        stmt = stmt.where(R.legal_entity_id == filters.legal_entity_id)
    if filters.bank_account_id:
        stmt = stmt.where(R.bank_account_id == filters.bank_account_id)
    if filters.currency_code:
        stmt = stmt.where(BankAccount.currency_code == filters.currency_code)
    if filters.run_status:
        stmt = stmt.where(R.status.in_(filters.run_status))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(R.created_at.desc(), R.id)
    if filters.paginate:
        stmt = stmt.limit(filters.page_size).offset((filters.page - 1) * filters.page_size)
    elif total > MAX_EXPORT_ROWS:
        raise HTTPException(status_code=422, detail="Too many rows to export; narrow the filters.")
    S, G, I = ReconciliationMatchSuggestion, ReconciliationMatchGroup, ReconciliationOpenItem
    rows = []
    totals = {"bank": 0, "ledger": 0, "accepted": 0, "pending": 0, "open": 0}
    for run, account, entity_name in (await db.execute(stmt)).all():
        pop = (await population(db, scope, [run.bank_account_id], run.period_start, run.period_end, None))[run.bank_account_id]
        pending = (await db.execute(select(func.count()).select_from(S).where(S.reconciliation_run_id == run.id, _pending_decidable()))).scalar_one() \
            + (await db.execute(select(func.count()).select_from(G).where(G.reconciliation_run_id == run.id, G.status == MatchGroupStatus.PENDING))).scalar_one()
        rejected = (await db.execute(select(func.count()).select_from(S).where(S.reconciliation_run_id == run.id, S.status == MatchSuggestionStatus.REJECTED))).scalar_one() \
            + (await db.execute(select(func.count()).select_from(G).where(G.reconciliation_run_id == run.id, G.status == MatchGroupStatus.REJECTED))).scalar_one()
        by_status = dict((await db.execute(select(I.status, func.count()).where(I.reconciliation_run_id == run.id).group_by(I.status))).all())
        rows.append({
            "id": str(run.id), "reference": short_ref("RUN", run.id), "entity": entity_name,
            "bank_account": BankAccountOut.mask_account_number(account.account_number), "currency": account.currency_code,
            "period": f"{run.period_start} to {run.period_end}", "created": str(utc_date(run.created_at)),
            "executed": str(utc_date(run.completed_at or run.started_at)) if (run.completed_at or run.started_at) else None,
            "status": run.status.value, "bank_in_scope": pop["bank"]["total"], "ledger_in_scope": pop["ledger"]["total"],
            "accepted_bank": pop["bank"]["accepted"], "accepted_ledger": pop["ledger"]["accepted"],
            "unmatched_bank": pop["bank"]["unmatched"], "unmatched_ledger": pop["ledger"]["unmatched"],
            "proposed_bank": pop["bank"]["proposed"], "proposed_ledger": pop["ledger"]["proposed"],
            "pending_proposals": pending, "rejected_proposals": rejected,
            "exceptions_open": by_status.get(OpenItemStatus.OPEN, 0), "exceptions_under_review": by_status.get(OpenItemStatus.UNDER_REVIEW, 0),
            "exceptions_resolved": by_status.get(OpenItemStatus.RESOLVED, 0), "exceptions_closed": by_status.get(OpenItemStatus.CLOSED, 0),
            "_drill": {"report": "exceptions", "params": {"bank_account_id": str(run.bank_account_id)}},
        })
        totals["bank"] += pop["bank"]["total"]; totals["ledger"] += pop["ledger"]["total"]
    cols = [col("reference", "Run"), col("entity", "Entity"), col("bank_account", "Bank account"), col("currency", "Currency"),
            col("period", "Period"), col("created", "Created"), col("executed", "Executed"), col("status", "Status"),
            col("bank_in_scope", "Bank txns", "right"), col("ledger_in_scope", "Ledger txns", "right"),
            col("accepted_bank", "Accepted (bank)", "right"), col("accepted_ledger", "Accepted (ledger)", "right"),
            col("unmatched_bank", "Unmatched (bank)", "right"), col("unmatched_ledger", "Unmatched (ledger)", "right"),
            col("pending_proposals", "Pending proposals", "right"), col("rejected_proposals", "Rejected proposals", "right"),
            col("exceptions_open", "Open", "right"), col("exceptions_under_review", "Under review", "right"),
            col("exceptions_resolved", "Resolved", "right"), col("exceptions_closed", "Closed", "right")]
    result["kpis"] = [kpi("runs", "Runs in period", total, "Runs whose reconciliation period overlaps the reporting period (after filters).", "reconciliation_runs")]
    result["tables"] = [table("runs", "Runs", cols, rows, total_rows=total, page=filters.page if filters.paginate else None,
                              page_size=filters.page_size if filters.paginate else None)]
    result["notes"] = ["Counts are unique transactions; a transaction is counted once however many proposals mention it. Run rows are not summed, because runs over overlapping periods would count the same transactions twice."]
    return result


def _pending_decidable():
    """The match-review predicate (an unambiguous auto-tier one-to-one suggestion still PENDING), reused, not redefined."""
    from app.api.v1.reconciliation_match_proposals import _suggestion_pending_decidable

    return _suggestion_pending_decidable()


# ======================================================================================
# 3.2 matching performance
# ======================================================================================
PROPOSAL_TYPES = [("ONE_TO_ONE", "One-to-one"), ("ONE_TO_MANY", "One-to-many"), ("MANY_TO_ONE", "Many-to-one"),
                  ("BATCH", "Batch payment"), ("INTERNAL_TRANSFER", "Internal transfer"), ("FX_MATCH", "FX-aware")]


def _type_expr():
    D, G = ReconciliationMatchDecision, ReconciliationMatchGroup
    return case((D.kind == MatchProposalKind.SUGGESTION, "ONE_TO_ONE"), else_=cast(G.relationship_type, String))



async def pending_by_type(db, scope, filters, account_ids) -> dict[str, int]:
    """Proposals awaiting an explicit decision (the match-review queue), by type, for runs overlapping the period."""
    G, R, S = ReconciliationMatchGroup, ReconciliationRun, ReconciliationMatchSuggestion
    overlap = and_(R.bank_account_id.in_(account_ids or [uuid.uuid4()]), R.period_start <= filters.date_to, R.period_end >= filters.date_from)
    out = {"ONE_TO_ONE": (await db.execute(scope.apply(select(func.count()).select_from(S).join(R, R.id == S.reconciliation_run_id), R.legal_entity_id)
                                           .where(overlap, _pending_decidable()))).scalar_one()}
    for rel, n in (await db.execute(scope.apply(select(G.relationship_type, func.count()).select_from(G).join(R, R.id == G.reconciliation_run_id), R.legal_entity_id)
                                    .where(overlap, G.status == MatchGroupStatus.PENDING).group_by(G.relationship_type))).all():
        out[rel.value] = n
    return out


async def matching_performance_report(db, scope, filters) -> dict[str, Any]:
    D, G, R, C = ReconciliationMatchDecision, ReconciliationMatchGroup, ReconciliationRun, ReconciliationAcceptedClaim
    result = await new_result(db, scope, filters, report="matching-performance", title="Matching Performance", definitions=[
        "Two different measures are shown and must not be added together. DECISION COUNTS count proposals (a suggestion or a group). UNIQUE TRANSACTION COUNTS count distinct bank/ledger rows, each once.",
        "The per-type figures are not mutually exclusive: one ledger row can be in a one-to-one match and also in an internal-transfer pairing, so only the 'any type' totals are de-duplicated and may be summed.",
        "Accepted = explicit accept decision (claims table). Pending = proposal awaiting a decision (match-review queue); not a completed reconciliation. Rejected = explicit rejection.",
        "Decision counts cover runs whose reconciliation period overlaps the reporting period; unique transaction counts cover transactions dated inside the reporting period.",
        "Each eligible transaction is in exactly one bucket: accepted, proposed-only (pending proposal, not accepted) or unmatched.",
    ])
    accounts = await load_accounts(db, scope, filters)
    ids = [a["id"] for a in accounts]
    pops = await population(db, scope, ids, filters.date_from, filters.date_to, filters.currency_code)
    bank = _empty_side(); ledger = _empty_side(); amounts: dict[str, dict[str, Decimal]] = {}
    for pop in pops.values():
        for k in bank:
            bank[k] += pop["bank"][k]; ledger[k] += pop["ledger"][k]
        merge_amounts(amounts, pop["amounts"])

    overlap = and_(R.bank_account_id.in_(ids or [uuid.uuid4()]), R.period_start <= filters.date_to, R.period_end >= filters.date_from)
    te = _type_expr()
    decisions = {}
    stmt = (scope.apply(select(te, D.decision, func.count()).select_from(D).join(R, R.id == D.reconciliation_run_id)
                        .outerjoin(G, G.id == D.group_id), R.legal_entity_id).where(overlap).group_by(te, D.decision))
    for t, decision, n in (await db.execute(stmt)).all():
        decisions[(t, decision.value)] = n
    pending = await pending_by_type(db, scope, filters, ids)
    B, T = BankStatementTransaction, TreasuryTransaction
    uniq_bank: dict[str, int] = {}; uniq_ledger: dict[str, int] = {}
    bank_stmt = (scope.apply(select(te, func.count(func.distinct(C.bank_statement_transaction_id))).select_from(C)
                             .join(D, D.id == C.decision_id).outerjoin(G, G.id == D.group_id).join(B, B.id == C.bank_statement_transaction_id), B.legal_entity_id)
                 .where(B.status == BankStatementTransactionStatus.ACTIVE, B.bank_account_id.in_(ids or [uuid.uuid4()]), B.transaction_date >= filters.date_from,
                        B.transaction_date <= filters.date_to).group_by(te))
    led_stmt = (scope.apply(select(te, func.count(func.distinct(C.treasury_transaction_id))).select_from(C)
                            .join(D, D.id == C.decision_id).outerjoin(G, G.id == D.group_id).join(T, T.id == C.treasury_transaction_id), T.legal_entity_id)
                .where(T.status == TransactionStatus.POSTED, T.bank_account_id.in_(ids or [uuid.uuid4()]), T.event_date >= filters.date_from,
                       T.event_date <= filters.date_to).group_by(te))
    if filters.currency_code:
        bank_stmt = bank_stmt.where(B.currency_code == filters.currency_code); led_stmt = led_stmt.where(T.transaction_currency_code == filters.currency_code)
    uniq_bank = dict((await db.execute(bank_stmt)).all()); uniq_ledger = dict((await db.execute(led_stmt)).all())

    rows = []
    for key, label in PROPOSAL_TYPES:
        rows.append({"type": label, "accepted_decisions": decisions.get((key, "ACCEPTED"), 0),
                     "accepted_unique_bank": uniq_bank.get(key, 0), "accepted_unique_ledger": uniq_ledger.get(key, 0),
                     "pending_proposals": pending.get(key, 0), "rejected_proposals": decisions.get((key, "REJECTED"), 0)})
    acc_total, eligible = bank["accepted"] + ledger["accepted"], bank["total"] + ledger["total"]
    cols = [col("type", "Match type"), col("accepted_decisions", "Accepted (decisions)", "right"), col("accepted_unique_bank", "Accepted (unique bank txns)", "right"),
            col("accepted_unique_ledger", "Accepted (unique ledger txns)", "right"), col("pending_proposals", "Pending proposals", "right"),
            col("rejected_proposals", "Rejected proposals", "right")]
    bucket_rows = [{"side": side_label, **vals} for side_label, vals in (("Bank statement rows", bank), ("Ledger rows", ledger),
                    ("Both (de-duplicated total)", {k: bank[k] + ledger[k] for k in bank}))]
    cur_rows = [{"currency": cur, **{k: money(v) for k, v in sorted(parts.items())}} for cur, parts in sorted(amounts.items())]
    result["kpis"] = [
        kpi("eligible", "Eligible transactions (unique)", eligible, "ACTIVE bank rows + POSTED ledger rows dated in the period, each counted once.", "bank_statement_transactions, treasury_transactions"),
        kpi("accepted", "Accepted (unique transactions)", acc_total, "Eligible transactions with an accepted claim.", "reconciliation_accepted_claims"),
        kpi("proposed", "Proposed only (unique transactions)", bank["proposed"] + ledger["proposed"], "Not accepted, covered by a PENDING proposal.", "reconciliation_match_suggestions, reconciliation_match_groups"),
        kpi("unmatched", "Unmatched (unique transactions)", bank["unmatched"] + ledger["unmatched"], "Neither accepted nor covered by a pending proposal.", "derived"),
        kpi("completion", "Completion rate (%)", pct(acc_total, eligible), "accepted unique transactions / eligible unique transactions x 100.", "derived"),
    ]
    result["tables"] = [
        table("types", "By match type (decision counts and unique transactions - not additive)", cols, rows),
        table("buckets", "Unique transactions by outcome (mutually exclusive; accepted + proposed + unmatched = eligible)",
              [col("side", "Population"), col("total", "Eligible", "right"), col("accepted", "Accepted", "right"), col("proposed", "Proposed only", "right"), col("unmatched", "Unmatched", "right")], bucket_rows),
        table("currencies", "Amounts by currency (bank and ledger shown separately; no conversion)",
              [col("currency", "Currency")] + [col(f"{side}_{k}", f"{side.title()} {k}", "right") for side in ("bank", "ledger") for k in ("total", "accepted", "proposed", "unmatched")], cur_rows),
    ]
    return result


# ======================================================================================
# 3.3 open exceptions
# ======================================================================================
def age_expr(as_of: datetime.date):
    """Days from the UTC date the item was opened to its final disposition date, or to the as-of date while outstanding."""
    I = ReconciliationOpenItem
    created_d = cast(func.timezone("UTC", I.created_at), Date)
    ended = case((I.status.in_(FINAL), func.coalesce(I.closed_at, I.rejected_at, I.resolved_at)), else_=None)
    end_d = func.coalesce(cast(func.timezone("UTC", ended), Date), literal(as_of, Date))
    return type_coerce(end_d - created_d, Integer)


async def exceptions_report(db, scope, filters) -> dict[str, Any]:
    I, E = ReconciliationOpenItem, ReconciliationOpenItemEvent
    B, T = BankStatementTransaction, TreasuryTransaction
    result = await new_result(db, scope, filters, report="exceptions", title="Open Exceptions", period_based=False, definitions=[
        "An exception is an open item: an unresolved reconciliation difference that needs investigation. It is not a transaction and not a match proposal.",
        "By default only outstanding exceptions (OPEN, UNDER_REVIEW) are listed; choose other statuses to see resolved, closed or rejected items.",
        "Age in days = days from the UTC date the item was opened to the as-of date while outstanding, or to its resolution / closure / rejection date once final.",
        "Last investigation = the latest of: investigation started, comment, evidence, resolution, closure, rejection or reopening. Creation and assignment alone are not investigation activity.",
        "The reporting period is applied only when dates are supplied explicitly, and then to each item's TRANSACTION date; the transaction-date range filter narrows it further.",
        "Amounts keep their own currency and are never added across currencies.",
    ])
    statuses = filters.exception_status or list(ACTIONABLE)
    base = _exception_base(scope, filters, date_window=filters.period_explicit).where(I.status.in_(statuses))
    age = age_expr(filters.as_of).label("age_days")
    if filters.min_age_days is not None:
        base = base.where(age_expr(filters.as_of) >= filters.min_age_days)
    if filters.max_age_days is not None:
        base = base.where(age_expr(filters.as_of) <= filters.max_age_days)
    sub = base.subquery()
    total = (await db.execute(select(func.count()).select_from(sub))).scalar_one()
    if not filters.paginate and total > MAX_EXPORT_ROWS:
        raise HTTPException(status_code=422, detail="Too many rows to export; narrow the filters.")
    last_activity = (select(E.open_item_id.label("item_id"), func.max(E.created_at).label("last_at"))
                     .where(E.event_type.in_(ACTIVITY_EVENTS)).group_by(E.open_item_id).subquery())
    has_notes = exists().where(E.open_item_id == I.id, E.event_type.in_((OpenItemEventType.COMMENT, OpenItemEventType.EVIDENCE)))
    stmt = (scope.apply(select(I, age, LegalEntity.name, BankAccount.account_number, B.bank_reference, T.reference, User.full_name, last_activity.c.last_at, has_notes.label("notes"))
                        .join(LegalEntity, LegalEntity.id == I.legal_entity_id).join(BankAccount, BankAccount.id == I.bank_account_id)
                        .outerjoin(B, B.id == I.bank_statement_transaction_id).outerjoin(T, T.id == I.treasury_transaction_id)
                        .outerjoin(User, User.id == I.assigned_to_user_id).outerjoin(last_activity, last_activity.c.item_id == I.id), I.legal_entity_id)
            .where(I.id.in_(select(sub.c.id))).order_by(I.created_at, I.id))
    if filters.paginate:
        stmt = stmt.limit(filters.page_size).offset((filters.page - 1) * filters.page_size)
    rows = []
    for item, age_days, entity_name, acct_no, bank_ref, ledger_ref, assignee, last_at, notes in (await db.execute(stmt)).all():
        done = item.closed_at or item.rejected_at or item.resolved_at if item.status in FINAL else None
        rows.append({
            "id": str(item.id), "reference": short_ref("EXC", item.id), "entity": entity_name,
            "bank_account": BankAccountOut.mask_account_number(acct_no), "transaction_reference": bank_ref or ledger_ref or "-",
            "transaction_date": str(item.item_date), "amount": money(item.amount), "currency": item.currency_code,
            "category": item.category.value, "status": item.status.value, "assigned_to": assignee or "Unassigned",
            "opened": str(utc_date(item.created_at)), "last_investigation": str(utc_date(last_at)) if last_at else None,
            "age_days": age_days, "has_notes": "Yes" if notes else "No", "resolution_date": str(utc_date(done)) if done else None,
        })
    amounts: dict[str, Decimal] = {}
    for cur, amt in (await db.execute(select(sub.c.currency_code, func.sum(sub.c.amount)).group_by(sub.c.currency_code))).all():
        add_amount(amounts, cur, amt)
    cols = [col("reference", "Exception"), col("entity", "Entity"), col("bank_account", "Bank account"), col("transaction_reference", "Transaction ref"),
            col("transaction_date", "Txn date"), col("amount", "Amount", "right"), col("currency", "Currency"), col("category", "Category"),
            col("status", "Status"), col("assigned_to", "Assigned to"), col("opened", "Opened"), col("last_investigation", "Last investigation"),
            col("age_days", "Age (days)", "right"), col("has_notes", "Notes/history"), col("resolution_date", "Resolved/closed")]
    result["kpis"] = [kpi("exceptions", "Exceptions listed", total, "Items matching the filters.", "reconciliation_open_items")]
    result["tables"] = [table("exceptions", "Exceptions", cols, rows, total_rows=total, page=filters.page if filters.paginate else None,
                              page_size=filters.page_size if filters.paginate else None),
                        table("amounts", "Total amount by currency (listed exceptions; not converted)", [col("currency", "Currency"), col("amount", "Amount", "right")], currency_rows(amounts))]
    return result


# ======================================================================================
# 3.4 open-item ageing (status and assignee reconstructed from the event history)
# ======================================================================================
def _as_of_end(as_of: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(as_of + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.UTC)


def _latest_event_value(column, as_of: datetime.date):
    """The most recent event value at or before the end of `as_of` (UTC), per item: PostgreSQL DISTINCT ON."""
    E = ReconciliationOpenItemEvent
    return (select(E.open_item_id.label("item_id"), column.label("value"))
            .where(column.is_not(None), E.created_at < _as_of_end(as_of))
            .distinct(E.open_item_id).order_by(E.open_item_id, E.created_at.desc(), E.id.desc()).subquery())


async def outstanding_as_of(db, scope, filters) -> list[dict[str, Any]]:
    """Items that were OPEN or UNDER_REVIEW at the end of the as-of date, with their assignee at that time."""
    I = ReconciliationOpenItem
    status_sq = _latest_event_value(ReconciliationOpenItemEvent.to_status, filters.as_of)
    assignee_sq = _latest_event_value(ReconciliationOpenItemEvent.assignee_user_id, filters.as_of)
    base = _exception_base(scope, filters, date_window=filters.period_explicit, assignee_filter=False)
    stmt = (base.with_only_columns(I.id, I.legal_entity_id, I.bank_account_id, I.category, I.currency_code, I.amount, I.created_at,
                                   assignee_sq.c.value, LegalEntity.name, BankAccount.account_number)
            .join(status_sq, status_sq.c.item_id == I.id).outerjoin(assignee_sq, assignee_sq.c.item_id == I.id)
            .join(LegalEntity, LegalEntity.id == I.legal_entity_id).join(BankAccount, BankAccount.id == I.bank_account_id)
            .where(status_sq.c.value.in_(ACTIONABLE)).order_by(I.created_at, I.id))
    rows = []
    for item_id, entity_id, account_id, category, currency, amount, created_at, assignee, entity_name, acct_no in (await db.execute(stmt)).all():
        age = (filters.as_of - utc_date(created_at)).days
        if filters.min_age_days is not None and age < filters.min_age_days:
            continue
        if filters.max_age_days is not None and age > filters.max_age_days:
            continue
        if filters.unassigned and assignee is not None:
            continue
        if filters.assigned_to_user_id and assignee != filters.assigned_to_user_id:
            continue
        rows.append({"id": item_id, "entity_id": entity_id, "entity": entity_name, "account_id": account_id,
                     "account": BankAccountOut.mask_account_number(acct_no), "category": category.value, "currency": currency,
                     "amount": amount, "age": age, "band": band_for(age), "assigned": assignee is not None})
    return rows


def _band_matrix(rows: list[dict], key_fn, label_cols: list[tuple[str, str]], key_label) -> tuple[list[dict], list[dict]]:
    groups: dict[Any, dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(key_fn(r), {"label": key_label(r), "counts": {b[0]: 0 for b in AGEING_BANDS}, "total": 0, "amounts": {}})
        g["counts"][r["band"]] += 1; g["total"] += 1; add_amount(g["amounts"], r["currency"], r["amount"])
    out = []
    for g in sorted(groups.values(), key=lambda x: x["label"]):
        row = {"name": g["label"], **{f"band_{k}": v for k, v in g["counts"].items()}, "total": g["total"], "amounts": currency_text(g["amounts"])}
        out.append(row)
    cols = [col("name", label_cols[0][1])] + [col(f"band_{k}", label, "right") for k, _lo, _hi, label in AGEING_BANDS] + [col("total", "Total", "right"), col("amounts", "Amount by currency")]
    return cols, out


async def ageing_report(db, scope, filters) -> dict[str, Any]:
    result = await new_result(db, scope, filters, report="ageing", title="Open-Item Ageing", period_based=False, definitions=[
        "Outstanding = the item was OPEN or UNDER_REVIEW at the end of the as-of date (UTC). Status and assignee at that date are rebuilt from the item's event history, not from its current status.",
        "Age in days = as-of date minus the UTC date the item was opened (created). Bands: 0-7, 8-14, 15-30, 31-60 and more than 60 days.",
        "Assigned / unassigned = whether the item had been assigned (any ASSIGNED or REASSIGNED event) by the end of the as-of date.",
        "Amounts are shown per currency and are never added across currencies; no FX conversion is applied.",
        "Items created after the as-of date are excluded. The reporting period is applied only when dates are supplied explicitly, and then to each item's TRANSACTION date.",
    ])
    rows = await outstanding_as_of(db, scope, filters)
    band_rows, band_cur = [], []
    for key, _lo, _hi, label in AGEING_BANDS:
        in_band = [r for r in rows if r["band"] == key]
        amounts: dict[str, Decimal] = {}
        for r in in_band:
            add_amount(amounts, r["currency"], r["amount"])
        band_rows.append({"band": label, "items": len(in_band), "assigned": sum(1 for r in in_band if r["assigned"]),
                          "unassigned": sum(1 for r in in_band if not r["assigned"]), "amounts": currency_text(amounts)})
        for cur, amt in sorted(amounts.items()):
            band_cur.append({"band": label, "currency": cur, "items": sum(1 for r in in_band if r["currency"] == cur), "amount": money(amt)})
    acct_cols, acct_rows = _band_matrix(rows, lambda r: (r["entity"], r["account"]), [("n", "Bank account")], lambda r: f"{r['entity']} / {r['account']}")
    ent_cols, ent_rows = _band_matrix(rows, lambda r: r["entity"], [("n", "Entity")], lambda r: r["entity"])
    cat_cols, cat_rows = _band_matrix(rows, lambda r: r["category"], [("n", "Exception category")], lambda r: r["category"])
    oldest = max((r["age"] for r in rows), default=None)
    over60 = sum(1 for r in rows if r["band"] == "60+")
    result["kpis"] = [
        kpi("outstanding", "Outstanding items", len(rows), "Items OPEN or UNDER_REVIEW at the end of the as-of date.", "reconciliation_open_items + reconciliation_open_item_events",
            {"report": "exceptions", "params": {}}),
        kpi("unassigned", "Unassigned", sum(1 for r in rows if not r["assigned"]), "Outstanding items with no assignment event by the as-of date.", "reconciliation_open_item_events",
            {"report": "exceptions", "params": {"unassigned": "true"}}),
        kpi("over60", "More than 60 days", over60, "Outstanding items aged 61+ days.", "reconciliation_open_items", {"report": "exceptions", "params": {"min_age_days": "61"}}),
        kpi("oldest", "Oldest item (days)", oldest, "Greatest age among outstanding items.", "reconciliation_open_items"),
    ]
    result["tables"] = [
        table("bands", "Ageing bands", [col("band", "Age band"), col("items", "Items", "right"), col("assigned", "Assigned", "right"), col("unassigned", "Unassigned", "right"), col("amounts", "Amount by currency")], band_rows),
        table("band_currency", "Amount per currency per band (never converted)", [col("band", "Age band"), col("currency", "Currency"), col("items", "Items", "right"), col("amount", "Amount", "right")], band_cur),
        table("by_account", "Ageing by bank account", acct_cols, acct_rows), table("by_entity", "Ageing by entity", ent_cols, ent_rows),
        table("by_category", "Ageing by exception category", cat_cols, cat_rows),
    ]
    return result


# ======================================================================================
# 3.5 assignment and investigation
# ======================================================================================
async def assignment_report(db, scope, filters) -> dict[str, Any]:
    I, E = ReconciliationOpenItem, ReconciliationOpenItemEvent
    result = await new_result(db, scope, filters, report="assignment", title="Assignment and Investigation", period_based=False, definitions=[
        "Counts use each item's CURRENT status and current assignee, taken from the item itself; resolution and activity measures use the event history.",
        "Average resolution time = mean time from an item's creation to its FIRST resolution event, over items that have been resolved at least once. It is shown in hours and days with the number of items measured.",
        "Investigation activity = investigation started, comment, evidence, resolution, closure, rejection or reopening. An assignment alone is not investigation.",
        "Reopened = items that have at least one reopen event. Reopen events are counted as recorded; nothing is inferred.",
        "The reporting period is applied only when dates are supplied explicitly, and then to each item's TRANSACTION date (the same rule as the exceptions and ageing reports); without explicit dates all items are included.",
    ])
    base = _exception_base(scope, filters, date_window=filters.period_explicit)
    if filters.exception_status:
        base = base.where(I.status.in_(filters.exception_status))
    sub = base.subquery()
    items = (await db.execute(select(I, User.full_name).outerjoin(User, User.id == I.assigned_to_user_id).where(I.id.in_(select(sub.c.id))).order_by(I.created_at, I.id))).all()
    first_resolved = dict((await db.execute(select(E.open_item_id, func.min(E.created_at)).where(E.event_type == OpenItemEventType.RESOLVED, E.open_item_id.in_(select(sub.c.id))).group_by(E.open_item_id))).all())
    active = {r[0] for r in (await db.execute(select(E.open_item_id).where(E.event_type.in_(ACTIVITY_EVENTS), E.open_item_id.in_(select(sub.c.id))).distinct())).all()}
    reopen_counts = dict((await db.execute(select(E.open_item_id, func.count()).where(E.event_type == OpenItemEventType.REOPENED, E.open_item_id.in_(select(sub.c.id))).group_by(E.open_item_id))).all())

    by_status = {s: 0 for s in OpenItemStatus}
    by_assignee: dict[str, dict[str, int]] = {}
    outstanding = [(i, n) for i, n in items if i.status in ACTIONABLE]
    for item, name in items:
        by_status[item.status] += 1
        row = by_assignee.setdefault(name or "Unassigned", {s.value: 0 for s in OpenItemStatus})
        row[item.status.value] += 1
    assigned_out = sum(1 for i, _ in outstanding if i.assigned_to_user_id)
    durations = [(first_resolved[i.id] - i.created_at).total_seconds() for i, _ in items if i.id in first_resolved]
    avg_hours = avg_days = None
    if durations:
        mean = Decimal(sum(durations)) / Decimal(len(durations))
        avg_hours = format((mean / 3600).quantize(Decimal("0.01"), ROUND_HALF_UP), "f")
        avg_days = format((mean / 86400).quantize(Decimal("0.01"), ROUND_HALF_UP), "f")
    no_activity = [(i, n) for i, n in outstanding if i.id not in active]
    ref_day = filters.as_of

    def oldest_rows(pairs):
        return [{"reference": short_ref("EXC", i.id), "category": i.category.value, "status": i.status.value, "assigned_to": n or "Unassigned",
                 "opened": str(utc_date(i.created_at)), "age_days": (ref_day - utc_date(i.created_at)).days,
                 "amount": money(i.amount), "currency": i.currency_code} for i, n in sorted(pairs, key=lambda p: (p[0].created_at, p[0].id))[:10]]
    out_cols = [col("reference", "Exception"), col("category", "Category"), col("status", "Status"), col("assigned_to", "Assigned to"),
                col("opened", "Opened"), col("age_days", "Age (days)", "right"), col("amount", "Amount", "right"), col("currency", "Currency")]
    result["kpis"] = [
        kpi("assigned", "Assigned (outstanding)", assigned_out, "Outstanding items (OPEN/UNDER_REVIEW) with an assignee.", "reconciliation_open_items"),
        kpi("unassigned", "Unassigned (outstanding)", len(outstanding) - assigned_out, "Outstanding items with no assignee.", "reconciliation_open_items", {"report": "exceptions", "params": {"unassigned": "true"}}),
        kpi("under_review", "Under review", by_status[OpenItemStatus.UNDER_REVIEW], "Items currently UNDER_REVIEW.", "reconciliation_open_items"),
        kpi("resolved", "Resolved", by_status[OpenItemStatus.RESOLVED], "Items currently RESOLVED (awaiting closure).", "reconciliation_open_items"),
        kpi("closed", "Closed", by_status[OpenItemStatus.CLOSED], "Items currently CLOSED.", "reconciliation_open_items"),
        kpi("avg_resolution_hours", "Average resolution (hours)", avg_hours, "Mean of (first RESOLVED event - creation) over items resolved at least once.", "reconciliation_open_item_events"),
        kpi("no_activity", "No investigation activity", len(no_activity), "Outstanding items with no investigation event.", "reconciliation_open_item_events"),
        kpi("reopened", "Items reopened", sum(1 for _ in reopen_counts), "Items with at least one REOPENED event.", "reconciliation_open_item_events"),
    ]
    assignee_cols = [col("assignee", "Team member")] + [col(s.value.lower(), s.value.replace("_", " ").title(), "right") for s in OpenItemStatus] + [col("total", "Total", "right")]
    assignee_rows = [{"assignee": n, **{k.lower(): v for k, v in c.items()}, "total": sum(c.values())} for n, c in sorted(by_assignee.items())]
    result["tables"] = [
        table("by_assignee", "Items by assigned team member (current status)", assignee_cols, assignee_rows),
        table("resolution", "Resolution time", [col("measured", "Items measured", "right"), col("avg_hours", "Average (hours)", "right"), col("avg_days", "Average (days)", "right")],
              [{"measured": len(durations), "avg_hours": avg_hours, "avg_days": avg_days}], note=None if durations else "No resolved items in scope; the average is undefined, not zero."),
        table("oldest", "Oldest outstanding items (top 10)", out_cols, oldest_rows(outstanding)),
        table("no_activity", "Outstanding items with no investigation activity (oldest 10)", out_cols, oldest_rows(no_activity)),
        table("reopened", "Reopening", [col("items", "Items reopened", "right"), col("events", "Reopen events", "right")], [{"items": len(reopen_counts), "events": sum(reopen_counts.values())}]),
    ]
    result["notes"] = ["Unassignment is not a workflow action in Stage 5E, so no unassignment history exists to report."]
    return result


# ======================================================================================
# 3.6 bank account reconciliation
# ======================================================================================
async def _account_facts(db, scope, filters, accounts):
    ids = [a["id"] for a in accounts]
    return (await population(db, scope, ids, filters.date_from, filters.date_to, filters.currency_code),
            await account_exception_stats(db, scope, filters, ids), await account_run_stats(db, scope, filters, ids))


async def bank_account_report(db, scope, filters) -> dict[str, Any]:
    result = await new_result(db, scope, filters, report="bank-accounts", title="Bank Account Reconciliation", definitions=[
        "Eligible transactions = ACTIVE bank statement rows + POSTED ledger rows for the account, dated inside the reporting period, each counted once.",
        "Completion rate = (accepted bank rows + accepted ledger rows) / (eligible bank rows + eligible ledger rows) x 100. It uses unique transactions and accepted claims only - never proposal counts. Undefined (blank) when there are no eligible transactions.",
        "An account is 'reconciled in the period' when it has at least one COMPLETED run whose reconciliation period overlaps the reporting period. Otherwise it is shown as NOT YET RECONCILED and its accepted / exception / completion figures are left blank: a missing run is not the same as zero exceptions.",
        "Outstanding exceptions = OPEN or UNDER_REVIEW items on transactions dated in the period, counted once per underlying exception (distinct source). Unassigned = those with no assignee.",
        "Account numbers are masked (last four digits only). Latest completed date is the most recent completion of any COMPLETED run for the account.",
    ])
    accounts = await load_accounts(db, scope, filters)
    pops, exc, runs = await _account_facts(db, scope, filters, accounts)
    rows, tot = [], {"acc": 0, "elig": 0, "out": 0, "un": 0, "rec": 0}
    for a in accounts:
        pop, ex, rn = side_sum(pops[a["id"]]), exc[a["id"]], runs[a["id"]]
        reconciled = rn["completed_in_period"] > 0
        latest = rn["latest"]
        oldest_age = (filters.as_of - utc_date(ex["oldest_opened"])).days if reconciled and ex["oldest_opened"] else None
        tot["elig"] += pop["total"]; tot["acc"] += pop["accepted"]
        if reconciled:
            tot["rec"] += 1; tot["out"] += ex["outstanding"]; tot["un"] += ex["unassigned"]
        rows.append({
            "id": str(a["id"]), "entity": a["entity_name"], "bank": a["bank_name"], "account": a["masked"], "currency": a["currency"],
            "state": "Reconciled" if reconciled else "NOT YET RECONCILED",
            "latest_run": f"{short_ref('RUN', latest.id)} ({latest.status.value}, {utc_date(latest.created_at)})" if latest else "No run",
            "latest_completed": str(utc_date(rn["latest_completed_at"])) if rn["latest_completed_at"] else None,
            "completed_runs": rn["completed_in_period"], "eligible": pop["total"],
            "accepted": pop["accepted"] if reconciled else None, "accepted_bank": pops[a["id"]]["bank"]["accepted"] if reconciled else None,
            "accepted_ledger": pops[a["id"]]["ledger"]["accepted"] if reconciled else None,
            "outstanding": ex["outstanding"] if reconciled else None, "unassigned": ex["unassigned"] if reconciled else None,
            "oldest_open_days": oldest_age, "completion_rate": pct(pop["accepted"], pop["total"]) if reconciled else None,
            "_drill": {"report": "exceptions", "params": {"bank_account_id": str(a["id"])}},
        })
    total = len(rows)
    if filters.paginate:
        rows = rows[(filters.page - 1) * filters.page_size: filters.page * filters.page_size]
    elif total > MAX_EXPORT_ROWS:
        raise HTTPException(status_code=422, detail="Too many rows to export; narrow the filters.")
    result["kpis"] = [
        kpi("accounts", "Accounts in scope", total, "Active (not deactivated) bank accounts matching the filters.", "bank_accounts"),
        kpi("reconciled", "Reconciled in period", tot["rec"], "Accounts with a COMPLETED run overlapping the period.", "reconciliation_runs"),
        kpi("not_reconciled", "Not yet reconciled", total - tot["rec"], "Accounts with no COMPLETED run overlapping the period.", "reconciliation_runs"),
        kpi("outstanding", "Outstanding exceptions", tot["out"], "OPEN/UNDER_REVIEW items on period transactions, reconciled accounts only.", "reconciliation_open_items", {"report": "exceptions", "params": {}}),
        kpi("completion", "Completion rate (%)", pct(tot["acc"], tot["elig"]), "accepted unique transactions / eligible unique transactions x 100, all accounts in scope.", "derived"),
    ]
    cols = [col("entity", "Entity"), col("bank", "Bank"), col("account", "Account"), col("currency", "Currency"), col("state", "State"),
            col("latest_run", "Latest run"), col("latest_completed", "Latest completed"), col("completed_runs", "Completed runs in period", "right"),
            col("eligible", "Eligible txns", "right"), col("accepted_bank", "Accepted (bank)", "right"), col("accepted_ledger", "Accepted (ledger)", "right"),
            col("outstanding", "Outstanding exceptions", "right"), col("unassigned", "Unassigned", "right"), col("oldest_open_days", "Oldest open (days)", "right"),
            col("completion_rate", "Completion (%)", "right")]
    result["tables"] = [table("accounts", "Bank accounts", cols, rows, total_rows=total, page=filters.page if filters.paginate else None,
                              page_size=filters.page_size if filters.paginate else None)]
    return result


# ======================================================================================
# 3.7 entity and group dashboards
# ======================================================================================
async def entity_metrics(db, scope, filters) -> list[dict[str, Any]]:
    accounts = await load_accounts(db, scope, filters)
    pops, exc, runs = await _account_facts(db, scope, filters, accounts)
    entities: dict[uuid.UUID, dict[str, Any]] = {e.id: {"id": e.id, "name": e.name, "code": e.code, "group_id": e.group_id, "accounts": 0, "reconciled": 0,
                       "pop": {"total": 0, "accepted": 0, "proposed": 0, "unmatched": 0}, "outstanding": 0, "unassigned": 0,
                       "ageing": {b[0]: 0 for b in AGEING_BANDS}, "amounts": {}} for e in await entity_rows(db, scope, filters)}
    for a in accounts:
        m = entities.get(a["entity_id"])
        if m is None:
            continue
        m["accounts"] += 1
        for k, v in side_sum(pops[a["id"]]).items():
            m["pop"][k] += v
        if runs[a["id"]]["completed_in_period"] > 0:
            m["reconciled"] += 1; m["outstanding"] += exc[a["id"]]["outstanding"]; m["unassigned"] += exc[a["id"]]["unassigned"]
            for cur, amt in exc[a["id"]]["amounts"].items():
                add_amount(m["amounts"], cur, amt)
    I = ReconciliationOpenItem
    base = _exception_base(scope, filters, date_window=True).where(I.status.in_(ACTIONABLE), I.bank_account_id.in_([a["id"] for a in accounts] or [uuid.uuid4()]))
    seen: dict[tuple, int] = {}
    reconciled_accounts = {a["id"] for a in accounts if runs[a["id"]]["completed_in_period"] > 0}
    sub = base.subquery()
    for entity_id, account_id, source_key, age in (await db.execute(select(sub.c.legal_entity_id, sub.c.bank_account_id, sub.c.source_key,
                                                                          type_coerce(literal(filters.as_of, Date) - cast(func.timezone("UTC", sub.c.created_at), Date), Integer)))).all():
        if account_id in reconciled_accounts:
            seen[(entity_id, source_key)] = max(seen.get((entity_id, source_key), 0), age)
    for (entity_id, _k), age in seen.items():
        entities[entity_id]["ageing"][band_for(age)] += 1
    for m in entities.values():
        m["not_reconciled"] = m["accounts"] - m["reconciled"]
        m["completion_rate"] = pct(m["pop"]["accepted"], m["pop"]["total"])
    return sorted(entities.values(), key=lambda m: m["name"])


def _dash_rows(metrics) -> list[dict]:
    return [{"entity": m["name"], "code": m["code"], "accounts": m["accounts"], "reconciled": m["reconciled"], "not_reconciled": m["not_reconciled"],
             "eligible": m["pop"]["total"], "accepted": m["pop"]["accepted"], "outstanding": m["outstanding"], "unassigned": m["unassigned"],
             "completion_rate": m["completion_rate"], "amounts": currency_text(m["amounts"]),
             "_drill": {"report": "exceptions", "params": {"legal_entity_id": str(m["id"])}}} for m in metrics]


_DASH_COLS = [col("entity", "Entity"), col("accounts", "Accounts", "right"), col("reconciled", "Reconciled", "right"), col("not_reconciled", "Not yet reconciled", "right"),
              col("eligible", "Eligible txns", "right"), col("accepted", "Accepted txns", "right"), col("outstanding", "Outstanding exceptions", "right"),
              col("unassigned", "Unassigned", "right"), col("completion_rate", "Completion (%)", "right"), col("amounts", "Outstanding amount by currency")]
_DASH_DEFS = [
    "Eligible, accepted and completion figures use unique transactions (ACTIVE bank rows + POSTED ledger rows dated in the period). Completion rate = accepted / eligible x 100, computed from summed counts - never an average of rates.",
    "Accounts reconciled = accounts with a COMPLETED run overlapping the period. Accounts not yet reconciled contribute their eligible transactions (so they lower the completion rate honestly) but no exception figures: a missing run is not zero exceptions.",
    "Outstanding exceptions are OPEN/UNDER_REVIEW items on period transactions in reconciled accounts, counted once per underlying exception. Amounts are per currency and never converted or added across currencies.",
    "Only entities the signed-in user is authorized to see are included; figures are not a complete group total unless the user is authorized for every entity.",
]


def _dash_kpis(metrics) -> list[dict]:
    t = {k: sum(m[k] for m in metrics) for k in ("accounts", "reconciled", "not_reconciled", "outstanding", "unassigned")}
    elig = sum(m["pop"]["total"] for m in metrics); acc = sum(m["pop"]["accepted"] for m in metrics)
    return [kpi("accounts", "Accounts in scope", t["accounts"], "Active bank accounts matching the filters.", "bank_accounts"),
            kpi("reconciled", "Reconciled in period", t["reconciled"], "Accounts with a COMPLETED run overlapping the period.", "reconciliation_runs"),
            kpi("not_reconciled", "Not yet reconciled", t["not_reconciled"], "Accounts with no COMPLETED run overlapping the period.", "reconciliation_runs"),
            kpi("eligible", "Eligible transactions (unique)", elig, "ACTIVE bank + POSTED ledger rows dated in the period.", "bank_statement_transactions, treasury_transactions"),
            kpi("accepted", "Accepted transactions (unique)", acc, "Eligible transactions with an accepted claim.", "reconciliation_accepted_claims"),
            kpi("outstanding", "Outstanding exceptions", t["outstanding"], "OPEN/UNDER_REVIEW items, reconciled accounts.", "reconciliation_open_items", {"report": "exceptions", "params": {}}),
            kpi("unassigned", "Unassigned exceptions", t["unassigned"], "Outstanding with no assignee.", "reconciliation_open_items", {"report": "exceptions", "params": {"unassigned": "true"}}),
            kpi("completion", "Completion rate (%)", pct(acc, elig), "accepted / eligible x 100.", "derived")]


def _ageing_table(metrics) -> dict:
    rows = [{"entity": m["name"], **{f"band_{k}": v for k, v in m["ageing"].items()}, "total": sum(m["ageing"].values())} for m in metrics]
    return table("ageing", "Ageing of outstanding exceptions by entity",
                 [col("entity", "Entity")] + [col(f"band_{k}", label, "right") for k, _l, _h, label in AGEING_BANDS] + [col("total", "Total", "right")], rows)


async def entity_dashboard_report(db, scope, filters) -> dict[str, Any]:
    result = await new_result(db, scope, filters, report="entity-dashboard", title="Entity Reconciliation Dashboard", definitions=_DASH_DEFS)
    metrics = await entity_metrics(db, scope, filters)
    result["kpis"] = _dash_kpis(metrics)
    result["tables"] = [table("entities", "Entities", _DASH_COLS, _dash_rows(metrics)), _ageing_table(metrics)]
    return result


async def group_dashboard_report(db, scope, filters) -> dict[str, Any]:
    from app.models.entity import Group

    result = await new_result(db, scope, filters, report="group-dashboard", title="Group Reconciliation Dashboard", definitions=_DASH_DEFS, group_level=True)
    metrics = await entity_metrics(db, scope, filters)
    names = {g.id: g.name for g in (await db.execute(select(Group).where(Group.id.in_({m["group_id"] for m in metrics} or {uuid.uuid4()})))).scalars()}
    group_rows = []
    for gid in sorted({m["group_id"] for m in metrics}, key=lambda g: names.get(g, "")):
        members = [m for m in metrics if m["group_id"] == gid]
        elig = sum(m["pop"]["total"] for m in members); acc = sum(m["pop"]["accepted"] for m in members)
        amounts: dict[str, Decimal] = {}
        for m in members:
            for cur, amt in m["amounts"].items():
                add_amount(amounts, cur, amt)
        group_rows.append({"group": names.get(gid, "-"), "entities": len(members), "accounts": sum(m["accounts"] for m in members),
                           "reconciled": sum(m["reconciled"] for m in members), "not_reconciled": sum(m["not_reconciled"] for m in members),
                           "eligible": elig, "accepted": acc, "outstanding": sum(m["outstanding"] for m in members),
                           "unassigned": sum(m["unassigned"] for m in members), "completion_rate": pct(acc, elig), "amounts": currency_text(amounts)})
    group_cols = [col("group", "Group"), col("entities", "Entities (authorized)", "right")] + _DASH_COLS[1:]
    result["kpis"] = _dash_kpis(metrics)
    result["tables"] = [table("groups", "Consolidated (authorized entities only)", group_cols, group_rows),
                        table("entities", "Entity comparison", _DASH_COLS, _dash_rows(metrics)), _ageing_table(metrics)]
    result["notes"] = ["Consolidation sums counts and keeps amounts separate per currency. The group figures cover only the entities the signed-in user is authorized to see."]
    return result


async def overview_report(db, scope, filters) -> dict[str, Any]:
    result = await new_result(db, scope, filters, report="overview", title="Reconciliation Reports Overview", definitions=_DASH_DEFS)
    metrics = await entity_metrics(db, scope, filters)
    accounts = await load_accounts(db, scope, filters)
    pending = await pending_by_type(db, scope, filters, [a["id"] for a in accounts])
    cards = _dash_kpis(metrics)
    cards.insert(5, kpi("pending", "Proposals awaiting a decision", sum(pending.values()), "PENDING proposals in the match-review queue (not counted as reconciled).",
                        "reconciliation_match_suggestions, reconciliation_match_groups"))
    result["kpis"] = cards
    result["tables"] = [table("entities", "Entities", _DASH_COLS, _dash_rows(metrics))]
    return result


REPORTS = {
    "overview": overview_report, "run-summary": run_summary_report, "matching-performance": matching_performance_report,
    "exceptions": exceptions_report, "ageing": ageing_report, "assignment": assignment_report, "bank-accounts": bank_account_report,
    "entity-dashboard": entity_dashboard_report, "group-dashboard": group_dashboard_report,
}
EXPORTABLE = tuple(k for k in REPORTS if k != "overview")
