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
