"""
Cash position service (SECTION 8, 20, 21).

Deliberately basic: reads posted TreasuryTransactions and the latest
BankBalance per account and aggregates them. This is the data/service
foundation the future Daily Cash Position Engine and 13-Week Forecast
Engine will build on, not the full liquidity/funding-optimization engine
(explicitly out of scope for this stage, per SECTION 8 and SECTION 28).

Group-level aggregation sums entity-level positions but does not attempt
transfer elimination yet (SECTION 21: create the correct foundation for
it, not a full consolidation engine). Transfers are identifiable via
TreasuryTransaction.transfer_pair_id, which a future consolidation step
can use to eliminate intra-group double-counting.
"""
import datetime
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.balance import BankBalance
from app.models.banking import BankAccount
from app.models.lookup import CashDirection
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction


@dataclass
class CashPositionResult:
    total_cash: Decimal = Decimal(0)
    available_cash: Decimal = Decimal(0)
    overdraft: Decimal = Decimal(0)
    net_cash: Decimal = Decimal(0)
    account_count: int = 0
    by_currency: dict = field(default_factory=dict)


async def _latest_balances(
    db: AsyncSession,
    legal_entity_id: uuid.UUID | None = None,
    legal_entity_ids: list | None = None,
    bank_id: uuid.UUID | None = None,
    bank_account_id: uuid.UUID | None = None,
    currency_code: str | None = None,
    as_of: datetime.date | None = None,
) -> list:
    as_of = as_of or datetime.date.today()

    latest_date_subq = (
        select(
            BankBalance.bank_account_id,
            func.max(BankBalance.balance_date).label("max_date"),
        )
        .where(BankBalance.balance_date <= as_of)
        .group_by(BankBalance.bank_account_id)
        .subquery()
    )

    stmt = (
        select(BankBalance)
        .join(BankAccount, BankAccount.id == BankBalance.bank_account_id)
        .join(
            latest_date_subq,
            (BankBalance.bank_account_id == latest_date_subq.c.bank_account_id)
            & (BankBalance.balance_date == latest_date_subq.c.max_date),
        )
        .where(BankAccount.is_active.is_(True))
    )
    if legal_entity_id:
        stmt = stmt.where(BankAccount.legal_entity_id == legal_entity_id)
    if legal_entity_ids is not None:
        stmt = stmt.where(BankAccount.legal_entity_id.in_(legal_entity_ids))
    if bank_id:
        stmt = stmt.where(BankAccount.bank_id == bank_id)
    if bank_account_id:
        stmt = stmt.where(BankAccount.id == bank_account_id)
    if currency_code:
        stmt = stmt.where(BankBalance.currency_code == currency_code.upper())

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def calculate_cash_position(
    db: AsyncSession,
    legal_entity_id: uuid.UUID | None = None,
    legal_entity_ids: list | None = None,
    bank_id: uuid.UUID | None = None,
    bank_account_id: uuid.UUID | None = None,
    currency_code: str | None = None,
    as_of: datetime.date | None = None,
) -> CashPositionResult:
    balances = await _latest_balances(
        db,
        legal_entity_id=legal_entity_id,
        legal_entity_ids=legal_entity_ids,
        bank_id=bank_id,
        bank_account_id=bank_account_id,
        currency_code=currency_code,
        as_of=as_of,
    )

    result = CashPositionResult(account_count=len(balances))
    for balance in balances:
        closing = balance.closing_balance or Decimal(0)
        available = (
            balance.available_balance if balance.available_balance is not None else closing
        )

        result.total_cash += closing
        result.available_cash += available
        if closing < 0:
            result.overdraft += -closing
        result.by_currency[balance.currency_code] = (
            result.by_currency.get(balance.currency_code, Decimal(0)) + closing
        )

    result.net_cash = result.total_cash - result.overdraft
    return result


async def calculate_net_movement(
    db: AsyncSession,
    legal_entity_id: uuid.UUID | None,
    start_date: datetime.date,
    end_date: datetime.date,
) -> dict:
    stmt = (
        select(TreasuryTransaction.direction, func.sum(TreasuryTransaction.transaction_amount))
        .where(
            TreasuryTransaction.status == TransactionStatus.POSTED,
            TreasuryTransaction.event_date >= start_date,
            TreasuryTransaction.event_date <= end_date,
        )
        .group_by(TreasuryTransaction.direction)
    )
    if legal_entity_id:
        stmt = stmt.where(TreasuryTransaction.legal_entity_id == legal_entity_id)

    result = await db.execute(stmt)
    totals = {direction.value: Decimal(0) for direction in CashDirection}
    for direction, total in result.all():
        totals[direction.value] = total or Decimal(0)
    return totals


@dataclass
class OperationalCashPositionResult:
    """
    SECTION 1 (Stage 4 final financial-integrity patch): distinguishes

        A. externally reported bank cash  -> reported_available_balance,
           anchored to reported_balance_date (the BankBalance snapshot -
           never mutated by this module; still the sole source of "what
           the bank says we have")
        B. operational available cash     -> operational_available_cash
           (what this module computes and what placement/rebooking
           validation actually uses)
        C. treasury movements not yet reflected in that snapshot ->
           unreflected_inflows / unreflected_outflows (posted
           TreasuryTransaction rows on this account dated AFTER the
           reported balance's own balance_date)

    Formula (SECTION 1):
        operational_available_cash
            = reported_available_balance
            + unreflected_inflows
            - unreflected_outflows

    This is deliberately NOT a second cash ledger: TreasuryTransaction
    remains the only transaction table, BankBalance remains the only
    externally-reported-balance table, and BankBalance itself is never
    mutated here (SECTION 1: "do NOT simply mutate BankBalance"). This
    function only ever READS both and combines them at query time.
    """
    bank_account_id: uuid.UUID | None
    currency_code: str
    reported_balance_date: datetime.date | None
    reported_available_balance: Decimal
    unreflected_inflows: Decimal
    unreflected_outflows: Decimal
    operational_available_cash: Decimal


async def calculate_operational_available_cash(
    db: AsyncSession, bank_account_id: uuid.UUID,
) -> OperationalCashPositionResult:
    """
    SECTION 1/7: the authoritative "can this specific bank account
    actually fund this?" figure - used by investment placement and
    rebooking (and available for any future module with the same need,
    e.g. Stage 3 facilities, without duplicating this logic).

    See app.models.balance.BankBalance's own docstring for the explicit
    documented convention behind balance_date (SECTION 4 of the Stage 4
    final financial-integrity patch) - this function's anchoring logic
    below implements exactly that convention.

    Double-count protection (SECTION 7): a TreasuryTransaction is only
    counted as "unreflected" while its event_date is AFTER the latest
    BankBalance's own balance_date. Once a later balance import arrives
    dated on or after that transaction's event_date, the bank's own
    reported figure is assumed to already include it (the standard
    "anchor to the last statement date" treasury convention), so it
    naturally drops out of the unreflected sum - it is never subtracted
    twice. A transaction dated exactly ON the balance's own date is
    treated as already reflected (same-day statements are assumed
    current as of end of that day).
    """
    balances = await _latest_balances(db, bank_account_id=bank_account_id)
    if not balances:
        # No balance has ever been reported for this account - the only
        # information available is the treasury's own posted movements,
        # anchored to "the beginning of time" rather than inventing a
        # reported balance that doesn't exist.
        account = await db.get(BankAccount, bank_account_id)
        currency_code = account.currency_code if account else ""
        anchor_date = datetime.date.min
        reported_available = Decimal(0)
        reported_balance_date = None
    else:
        balance = balances[0]
        currency_code = balance.currency_code
        anchor_date = balance.balance_date
        reported_balance_date = balance.balance_date
        reported_available = (
            balance.available_balance if balance.available_balance is not None
            else balance.closing_balance or Decimal(0)
        )

    stmt = (
        select(TreasuryTransaction.direction, func.sum(TreasuryTransaction.transaction_amount))
        .where(
            TreasuryTransaction.bank_account_id == bank_account_id,
            TreasuryTransaction.status == TransactionStatus.POSTED,
            TreasuryTransaction.event_date > anchor_date,
            TreasuryTransaction.direction.in_([CashDirection.INFLOW, CashDirection.OUTFLOW]),
        )
        .group_by(TreasuryTransaction.direction)
    )
    result = await db.execute(stmt)
    totals = {CashDirection.INFLOW.value: Decimal(0), CashDirection.OUTFLOW.value: Decimal(0)}
    for direction, total in result.all():
        totals[direction.value] = total or Decimal(0)

    operational = reported_available + totals[CashDirection.INFLOW.value] - totals[CashDirection.OUTFLOW.value]

    return OperationalCashPositionResult(
        bank_account_id=bank_account_id, currency_code=currency_code,
        reported_balance_date=reported_balance_date, reported_available_balance=reported_available,
        unreflected_inflows=totals[CashDirection.INFLOW.value],
        unreflected_outflows=totals[CashDirection.OUTFLOW.value],
        operational_available_cash=operational,
    )
