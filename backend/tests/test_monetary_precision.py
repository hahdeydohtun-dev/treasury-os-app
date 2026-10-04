"""
Persisted monetary precision policy (app/core/monetary.py): two decimals system-wide.
Verifies against a real PostgreSQL database that supported amounts survive exactly,
and that unsupported precision is REFUSED rather than silently rounded.
"""
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_data_model import _login
from tests.test_reconciliation_fx_precision import _run_fx_scenario
from tests.test_reconciliation_matching_engine import _make_bank_statement_txn, _make_treasury_txn

# --- A: two-decimal values survive exactly (real database round trip) -------------------

async def test_two_decimal_amounts_round_trip_exactly(db_session: AsyncSession, demo_group_and_entities, cash_event_types):
    from app.models.bank_statement import BankStatementTransaction
    from app.models.treasury_transaction import TreasuryTransaction

    _, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="6161000011")
    ledger = await _make_treasury_txn(db_session, entity_id=entity_a.id, account_id=account.id, amount="100.25")
    stmt = await _make_bank_statement_txn(db_session, entity_id=entity_a.id, account_id=account.id, amount="100.25")
    ledger_id, stmt_id = ledger.id, stmt.id  # capture before expiring the identity map
    await db_session.commit()
    db_session.expire_all()
    assert (await db_session.get(TreasuryTransaction, ledger_id)).transaction_amount == Decimal("100.25")
    assert (await db_session.get(BankStatementTransaction, stmt_id)).amount == Decimal("100.25")


async def test_trailing_zero_representation_of_a_supported_amount_is_accepted(db_session, demo_group_and_entities, cash_event_types):
    _, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="6161000022")
    txn = await _make_treasury_txn(db_session, entity_id=entity_a.id, account_id=account.id, amount="100.250")
    assert txn.transaction_amount == Decimal("100.25")


# --- B/C: unsupported (3+ decimal) precision cannot enter the ledger ---------------------

async def test_three_decimal_ledger_amount_is_refused_not_rounded(db_session, demo_group_and_entities, cash_event_types):
    _, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="6161000033")
    with pytest.raises(ValueError, match="more than 2 decimal places"):
        await _make_treasury_txn(db_session, entity_id=entity_a.id, account_id=account.id, amount="123.456")


async def test_four_decimal_bank_statement_amount_is_refused_not_rounded(db_session, demo_group_and_entities, cash_event_types):
    _, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="6161000044")
    with pytest.raises(ValueError, match="more than 2 decimal places"):
        await _make_bank_statement_txn(db_session, entity_id=entity_a.id, account_id=account.id, amount="123.4567")


async def test_transaction_api_rejects_three_decimal_amount_and_accepts_two(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager, demo_group_and_entities,
):
    from app.models.lookup import CashDirection, CashEventType

    _, entity_a, _ = demo_group_and_entities
    if await db_session.get(CashEventType, "SUPPLIER_PAYMENT") is None:
        db_session.add(CashEventType(code="SUPPLIER_PAYMENT", name="Supplier Payment", default_direction=CashDirection.OUTFLOW))
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, group_wide_manager)}"}
    payload = {"legal_entity_id": str(entity_a.id), "event_type_code": "SUPPLIER_PAYMENT",
               "event_date": "2026-01-15", "transaction_currency_code": "NGN"}
    bad = await client.post("/api/v1/transactions", json={**payload, "transaction_amount": "123.456"}, headers=headers)
    assert bad.status_code == 422
    good = await client.post("/api/v1/transactions", json={**payload, "transaction_amount": "123.45"}, headers=headers)
    assert good.status_code == 201 and good.json()["transaction_amount"] == "123.45"


async def test_bank_statement_import_validation_rejects_excess_precision(db_session, demo_group_and_entities):
    from app.services.bank_statement_excel_template import _validate_bank_statement_row

    row = {"Entity": "x", "Bank Account": "x", "Transaction Date": "2026-06-10", "Debit/Credit": "CREDIT",
           "Currency": "NGN"}
    over = await _validate_bank_statement_row({**row, "Amount": "100.255"}, db_session)
    assert "AMOUNT_PRECISION_EXCEEDED" in {i.error_code for i in over}
    ok = await _validate_bank_statement_row({**row, "Amount": "100.25"}, db_session)
    assert "AMOUNT_PRECISION_EXCEEDED" not in {i.error_code for i in ok}
    over_balance = await _validate_bank_statement_row({**row, "Amount": "100.25", "Balance": "5.001"}, db_session)
    assert "AMOUNT_PRECISION_EXCEEDED" in {i.error_code for i in over_balance}


# --- Currency.decimal_places cannot declare unsupported precision -------------------------

@pytest.mark.parametrize("places", [3, 4, 6, -1])
async def test_database_refuses_currency_with_unsupported_decimal_places(db_session: AsyncSession, places):
    from app.models.currency import Currency

    db_session.add(Currency(code="ZZZ", name="Unsupported", decimal_places=places))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.parametrize("places", [0, 1, 2])
async def test_database_accepts_supported_currency_decimal_places(db_session: AsyncSession, places):
    from app.models.currency import Currency

    db_session.add(Currency(code="ZZY", name="Supported", decimal_places=places))
    await db_session.flush()


async def test_currency_api_rejects_three_decimal_places_and_accepts_two(client: AsyncClient, db_session: AsyncSession, superuser):
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, superuser)}"}
    resp = await client.post("/api/v1/currencies", headers=headers,
                             json={"code": "KWD", "name": "Kuwaiti Dinar", "decimal_places": 3})
    assert resp.status_code == 422
    ok = await client.post("/api/v1/currencies", headers=headers,
                           json={"code": "JPX", "name": "Zero decimal", "decimal_places": 0})
    assert ok.status_code == 201 and ok.json()["decimal_places"] == 0


async def test_live_database_column_types_match_policy(db_session: AsyncSession):
    rows = (await db_session.execute(text(
        "select table_name, column_name, numeric_precision, numeric_scale from information_schema.columns "
        "where table_schema='public' and (table_name, column_name) in "
        "(('treasury_transactions','transaction_amount'),('bank_statement_transactions','amount'),"
        "('reconciliation_match_groups','difference'),('reconciliation_match_groups','ledger_aggregate_amount'))"
    ))).all()
    assert len(rows) == 4 and all(r[2] == 20 and r[3] == 2 for r in rows)
    constraint = (await db_session.execute(text(
        "select pg_get_constraintdef(oid) from pg_constraint where conname='ck_currencies_decimal_places_supported'"
    ))).scalar_one()
    assert "decimal_places >= 0" in constraint and "decimal_places <= 2" in constraint


# --- D/E/F: FX persistence, difference in the ledger currency, tolerance ------------------

async def test_fx_group_persists_two_decimal_converted_precision_exactly(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    # 10000 USD * 1583.3333333333 = 15833333.33333... -> 15833333.33 (the target currency's 2 dp).
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="persist2dp", account_number="6161000055",
        target_currency="NGN", target_dp=2, rate="1583.3333333333", bank_amount="10000",
        ledger_amount="15833333.33", fx_tolerance_pct="0",
    )
    assert body["advanced_match_count"] == 1
    g = groups[0]
    assert g["ledger_aggregate_amount"] == "15833333.33" and g["difference"] == "0.00"
    assert "= 15833333.33 NGN" in g["reason"]


async def test_fx_difference_is_converted_minus_ledger_in_ledger_currency(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    # converted = 150123 JPY (0 dp); ledger = 150125 JPY -> difference 2.00 JPY. Raw subtraction of the
    # 1000 USD source amount from the 150125 JPY ledger amount would give 149125.00 - never that.
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="diffjpy", account_number="6161000066",
        target_currency="JPY", target_dp=0, rate="150.1234", bank_amount="1000", ledger_amount="150125",
        fx_tolerance_pct="1",
    )
    assert body["advanced_match_count"] == 1
    g = groups[0]
    assert g["difference"] == "2.00" and g["currency_code"] == "JPY" and g["bank_aggregate_amount"] == "1000.00"


async def test_fx_tolerance_within_and_outside(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    # Same 2.00 JPY gap: inside a 0.01% tolerance (allowed 15.01) and outside 0.0001% (allowed 0.15).
    within, _, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="tolin", account_number="6161000077",
        target_currency="JPY", target_dp=0, rate="150.1234", bank_amount="1000", ledger_amount="150125",
        fx_tolerance_pct="0.01",
    )
    outside, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="tolout", account_number="6161000088",
        target_currency="JPY", target_dp=0, rate="150.1234", bank_amount="1000", ledger_amount="150125",
        fx_tolerance_pct="0.0001", rate_date="2026-06-09",  # distinct rate identity from the first scenario
    )
    assert within["advanced_match_count"] == 1
    assert outside["advanced_match_count"] == 0 and groups == []
