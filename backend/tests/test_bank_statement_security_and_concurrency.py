import asyncio

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_xlsx_bytes
from tests.test_bank_statement_ingestion import (
    _login,
    _make_account,
    _make_bank,
    _make_scoped_user,
    _statement_row,
    _upload_and_get_batch,
)

# ---------------------------------------------------------------------------
# Authorization (SECTION 9/28)
# ---------------------------------------------------------------------------

async def test_entity_a_cannot_import_entity_b_bank_statement(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="9911223344")
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera-noimport",
    )
    await db_session.commit()
    token = await _login(client, user_a)
    headers = {"Authorization": f"Bearer {token}"}

    rows = [_statement_row("Entity B", account_b.account_number, bank_reference="REF-B-001")]
    resp = await _upload_and_get_batch(client, headers, entity_b.id, rows, make_xlsx_bytes)
    assert resp.status_code == 403


async def test_entity_a_cannot_read_entity_b_import_batch_or_statement_transactions(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="9911223355")
    user_b = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_b.id, label="userb-owner",
    )
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera-reader",
    )
    await db_session.commit()

    headers_b = {"Authorization": f"Bearer {await _login(client, user_b)}"}
    rows = [_statement_row("Entity B", account_b.account_number, bank_reference="REF-B-002")]
    upload_resp = await _upload_and_get_batch(client, headers_b, entity_b.id, rows, make_xlsx_bytes)
    batch_id = upload_resp.json()["id"]
    confirm_resp = await client.post(f"/api/v1/excel/imports/{batch_id}/confirm", headers=headers_b)
    assert confirm_resp.status_code == 200

    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    # Cannot read the batch/preview.
    validation_resp = await client.get(f"/api/v1/excel/validation/{batch_id}", headers=headers_a)
    assert validation_resp.status_code == 403

    # Cannot list Entity B's statement transactions via the entity filter.
    list_resp = await client.get(
        "/api/v1/bank-statements/transactions", params={"legal_entity_id": str(entity_b.id)}, headers=headers_a,
    )
    assert list_resp.status_code == 403

    # Cannot enumerate Entity B's statement transaction by direct ID either.
    unfiltered_list = await client.get("/api/v1/bank-statements/transactions", headers=headers_a)
    assert str(entity_b.id) not in {t["legal_entity_id"] for t in unfiltered_list.json()}

    txns_b = await client.get(
        "/api/v1/bank-statements/transactions", params={"legal_entity_id": str(entity_b.id)}, headers=headers_b,
    )
    txn_id = txns_b.json()[0]["id"]
    detail_resp = await client.get(f"/api/v1/bank-statements/transactions/{txn_id}", headers=headers_a)
    assert detail_resp.status_code == 403


async def test_entity_a_cannot_reference_entity_b_bank_account_during_import(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """
    A malicious upload naming Entity A in the batch's own scope but a row
    referencing Entity B's bank account text must still be rejected -
    server-side, never trusting the uploaded file's own claims.
    """
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="9911223366")
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera-crossacct",
    )
    await db_session.commit()
    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    # Row claims "Entity A" but points at Entity B's real bank account.
    rows = [_statement_row("Entity A", account_b.account_number, bank_reference="REF-CROSS-001")]
    upload_resp = await _upload_and_get_batch(client, headers_a, entity_a.id, rows, make_xlsx_bytes)
    batch = upload_resp.json()
    assert batch["invalid_rows"] == 1
    error_codes = {issue["error_code"] for issue in batch["issues"]}
    assert "BANK_ACCOUNT_NOT_AUTHORIZED" in error_codes


async def test_cross_group_bank_statement_access_denied(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.entity import Group, LegalEntity
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)

    group2 = Group(name="BankStmt Second Group", code="BSGRP2", reporting_currency_code="USD")
    db_session.add(group2)
    await db_session.flush()
    entity_c = LegalEntity(
        group_id=group2.id, name="BankStmt Entity C", code="BSENTC", functional_currency_code="USD", country="US",
    )
    db_session.add(entity_c)
    await db_session.flush()
    account_c = await _make_account(db_session, entity_c.id, bank.id, currency="USD", account_number="7788990011")

    user_c = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_c.id, label="userc",
    )
    group1_user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group.id, label="group1user",
    )
    await db_session.commit()

    headers_c = {"Authorization": f"Bearer {await _login(client, user_c)}"}
    rows = [_statement_row("BankStmt Entity C", account_c.account_number, currency="USD",
                            bank_reference="REF-C-001")]
    upload_resp = await _upload_and_get_batch(client, headers_c, entity_c.id, rows, make_xlsx_bytes)
    batch_id = upload_resp.json()["id"]
    await client.post(f"/api/v1/excel/imports/{batch_id}/confirm", headers=headers_c)

    headers_g1 = {"Authorization": f"Bearer {await _login(client, group1_user)}"}
    list_resp = await client.get(
        "/api/v1/bank-statements/transactions", params={"legal_entity_id": str(entity_c.id)}, headers=headers_g1,
    )
    assert list_resp.status_code == 403

    unfiltered = await client.get("/api/v1/bank-statements/transactions", headers=headers_g1)
    assert str(entity_c.id) not in {t["legal_entity_id"] for t in unfiltered.json()}


# ---------------------------------------------------------------------------
# Traceability (SECTION 17)
# ---------------------------------------------------------------------------

async def test_source_row_and_batch_traceability_preserved(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="traceability",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    rows = [
        _statement_row("Entity A", account.account_number, bank_reference="REF-TRACE-1"),
        _statement_row("Entity A", account.account_number, txn_date="2026-06-16", bank_reference="REF-TRACE-2"),
    ]
    upload_resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    batch_id = upload_resp.json()["id"]
    await client.post(f"/api/v1/excel/imports/{batch_id}/confirm", headers=headers)

    txns_resp = await client.get(
        "/api/v1/bank-statements/transactions", params={"import_batch_id": batch_id}, headers=headers,
    )
    txns = txns_resp.json()
    assert len(txns) == 2
    row_numbers = sorted(t["source_row_number"] for t in txns)
    assert row_numbers == [2, 3]  # excel row 2 and 3 (row 1 is the header)
    assert all(t["import_batch_id"] == batch_id for t in txns)


# ---------------------------------------------------------------------------
# Multi-currency (SECTION 19)
# ---------------------------------------------------------------------------

async def test_multi_currency_preserved_original_currency_never_converted(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    ngn_account = await _make_account(db_session, entity_a.id, bank.id, currency="NGN", account_number="1010101010")
    usd_account = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number="2020202020")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="multicurrency",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    rows = [
        _statement_row("Entity A", ngn_account.account_number, currency="NGN", bank_reference="REF-NGN-01"),
        _statement_row("Entity A", usd_account.account_number, currency="USD", bank_reference="REF-USD-01"),
    ]
    upload_resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    batch = upload_resp.json()
    assert batch["valid_rows"] == 2
    batch_id = batch["id"]
    await client.post(f"/api/v1/excel/imports/{batch_id}/confirm", headers=headers)

    txns_resp = await client.get(
        "/api/v1/bank-statements/transactions", params={"import_batch_id": batch_id}, headers=headers,
    )
    currencies = {t["currency_code"] for t in txns_resp.json()}
    assert currencies == {"NGN", "USD"}  # each preserved exactly, never converted/merged


# ---------------------------------------------------------------------------
# Idempotency (import-lifecycle addendum, SECTION 8)
# ---------------------------------------------------------------------------

async def test_repeated_confirmation_of_same_batch_is_idempotent(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="repeatconfirm",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    rows = [_statement_row("Entity A", account.account_number, bank_reference="REF-IDEMP-001")]
    upload_resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    batch_id = upload_resp.json()["id"]

    first = await client.post(f"/api/v1/excel/imports/{batch_id}/confirm", headers=headers)
    assert first.status_code == 200
    assert first.json()["imported_rows"] == 1

    # Double-click / retry: the second confirmation of the SAME batch
    # must not import a second time.
    second = await client.post(f"/api/v1/excel/imports/{batch_id}/confirm", headers=headers)
    assert second.status_code == 400

    from sqlalchemy import select

    from app.models.bank_statement import BankStatementTransaction

    result = await db_session.execute(
        select(BankStatementTransaction).where(BankStatementTransaction.bank_reference == "REF-IDEMP-001")
    )
    assert len(list(result.scalars().all())) == 1


# ---------------------------------------------------------------------------
# Concurrency (SECTION 9)
# ---------------------------------------------------------------------------

async def test_concurrent_confirmation_cannot_duplicate_records(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """
    Genuine concurrent requests (asyncio.gather, not sequential calls),
    mirroring the exact concurrency-test pattern already established for
    Stage 3/4 (e.g. two concurrent facility drawdowns, two concurrent
    investment placements).
    """
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="concurrentconfirm",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    rows = [_statement_row("Entity A", account.account_number, bank_reference="REF-CONCURRENT-001")]
    upload_resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    batch_id = upload_resp.json()["id"]

    results = await asyncio.gather(
        *[
            client.post(f"/api/v1/excel/imports/{batch_id}/confirm", headers=headers)
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    status_codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert status_codes.count(200) == 1
    assert 400 in status_codes

    from sqlalchemy import select

    from app.models.bank_statement import BankStatementTransaction

    result = await db_session.execute(
        select(BankStatementTransaction).where(BankStatementTransaction.bank_reference == "REF-CONCURRENT-001")
    )
    assert len(list(result.scalars().all())) == 1  # never duplicated by the race
