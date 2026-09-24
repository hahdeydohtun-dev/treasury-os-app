import datetime
import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User
from tests.conftest import make_xlsx_bytes


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _make_scoped_user(db_session, *, scope_type, legal_entity_id=None, group_id=None, label):
    from app.core.security import hash_password
    from app.models.rbac import Permission, Role, TreasuryAction, TreasuryModule, UserRoleAssignment

    role = Role(name=f"{label}-role-{uuid.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for module in (TreasuryModule.EXCEL_DATA_HUB, TreasuryModule.BANK_RECONCILIATION):
        for action in (TreasuryAction.VIEW, TreasuryAction.UPLOAD, TreasuryAction.IMPORT):
            db_session.add(Permission(role_id=role.id, module=module, action=action))
    user = User(
        email=f"{label}-{uuid.uuid4().hex[:8]}@treasuryos.example.com",
        full_name=label, hashed_password=hash_password("Password123!"), is_superuser=False,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserRoleAssignment(
        user_id=user.id, role_id=role.id, scope_type=scope_type,
        legal_entity_id=legal_entity_id, group_id=group_id,
    ))
    await db_session.flush()
    return user


async def _make_account_type(db_session):
    from app.models.lookup import AccountType

    if await db_session.get(AccountType, "CURRENT") is None:
        db_session.add(AccountType(code="CURRENT", name="Current Account"))
        await db_session.flush()


async def _make_bank(db_session, name="Stage5A Test Bank"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


async def _make_account(db_session, entity_id, bank_id, currency="NGN", account_number="0055667788"):
    from app.models.banking import BankAccount

    await _make_account_type(db_session)
    account = BankAccount(
        legal_entity_id=entity_id, bank_id=bank_id, account_name="Operating Account",
        account_number=account_number, currency_code=currency, account_type_code="CURRENT",
    )
    db_session.add(account)
    await db_session.flush()
    return account


def _statement_row(
    entity, account_number, period_start="2026-06-01", period_end="2026-06-30",
    txn_date="2026-06-15", entry_type="CREDIT", amount="1000000", currency="NGN",
    value_date=None, posting_date=None, balance=None, bank_reference=None,
    external_transaction_id=None, narration=None,
):
    return [
        entity, account_number, period_start, period_end, txn_date, entry_type, amount, currency,
        value_date or "", posting_date or "", balance if balance is not None else "",
        bank_reference or "", external_transaction_id or "", narration or "",
    ]


_HEADERS = [
    "Entity", "Bank Account", "Statement Period Start", "Statement Period End",
    "Transaction Date", "Debit/Credit", "Amount", "Currency",
    "Value Date", "Posting Date", "Balance", "Bank Reference",
    "External Transaction ID", "Narration",
]


async def _upload_and_get_batch(client, headers, entity_id, rows, make_xlsx_bytes):
    file_bytes = make_xlsx_bytes(_HEADERS, rows)
    resp = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "BANK_STATEMENT", "template_version": "1",
              "legal_entity_id": str(entity_id)},
        files={"file": ("statement.xlsx", file_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )
    return resp


async def test_bank_statement_transaction_model_valid_row(
    db_session: AsyncSession, demo_group_and_entities, group_wide_manager,
):
    import datetime as dt

    from app.models.bank_statement import BankStatementEntryType, BankStatementTransaction
    from app.models.excel_hub import ImportBatch, ImportBatchStatus

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    batch = ImportBatch(
        template_code="BANK_STATEMENT", template_version=1, legal_entity_id=entity_a.id,
        file_name="test.xlsx", uploaded_by_user_id=group_wide_manager.id,
        uploaded_at=dt.datetime.now(dt.UTC), status=ImportBatchStatus.IMPORTED,
    )
    db_session.add(batch)
    await db_session.flush()

    txn = BankStatementTransaction(
        legal_entity_id=entity_a.id, bank_id=bank.id, bank_account_id=account.id,
        statement_period_start=datetime.date(2026, 6, 1), statement_period_end=datetime.date(2026, 6, 30),
        transaction_date=datetime.date(2026, 6, 15), entry_type=BankStatementEntryType.CREDIT,
        amount=Decimal("1000000.00"), currency_code="NGN", duplicate_key="testkey123",
        has_strong_identity=False, import_batch_id=batch.id, source_row_number=2,
    )
    db_session.add(txn)
    await db_session.flush()
    assert txn.id is not None
    assert txn.status.value == "ACTIVE"


async def test_upload_alone_creates_no_durable_statement_transactions(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from sqlalchemy import select

    from app.models.bank_statement import BankStatementTransaction
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="uploadonly",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    rows = [_statement_row("Entity A", account.account_number, bank_reference="REF-001")]
    resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "READY_FOR_IMPORT"
    assert body["valid_rows"] == 1

    result = await db_session.execute(select(BankStatementTransaction))
    assert list(result.scalars().all()) == []


async def test_invalid_row_fails_validation_and_creates_no_statement_transaction(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from sqlalchemy import select

    from app.models.bank_statement import BankStatementTransaction
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="invalidrow",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    rows = [_statement_row("Entity A", account.account_number, amount="not-a-number")]
    resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    assert resp.status_code == 201
    body = resp.json()
    assert body["invalid_rows"] == 1
    error_codes = {issue["error_code"] for issue in body["issues"]}
    assert "INVALID_AMOUNT" in error_codes

    result = await db_session.execute(select(BankStatementTransaction))
    assert list(result.scalars().all()) == []


async def test_mixed_valid_invalid_file_partially_imports(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="mixedfile",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    rows = [
        _statement_row("Entity A", account.account_number, bank_reference="REF-A"),
        _statement_row("Entity A", account.account_number, amount="bad", bank_reference="REF-B"),
    ]
    upload_resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    batch = upload_resp.json()
    assert batch["valid_rows"] == 1
    assert batch["invalid_rows"] == 1

    confirm_resp = await client.post(f"/api/v1/excel/imports/{batch['id']}/confirm", headers=headers)
    assert confirm_resp.status_code == 200
    result = confirm_resp.json()
    assert result["imported_rows"] == 1
    assert result["skipped_rows"] == 1
    assert result["batch"]["status"] == "PARTIALLY_IMPORTED"


async def test_confirmed_import_creates_statement_transactions_and_never_treasury_transactions(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from sqlalchemy import select

    from app.models.bank_statement import BankStatementTransaction
    from app.models.rbac import EntityScopeType
    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="confirmimport",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    rows = [_statement_row("Entity A", account.account_number, bank_reference="REF-XYZ")]
    upload_resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    batch = upload_resp.json()

    confirm_resp = await client.post(f"/api/v1/excel/imports/{batch['id']}/confirm", headers=headers)
    assert confirm_resp.status_code == 200
    result = confirm_resp.json()
    assert result["imported_rows"] == 1
    assert result["batch"]["status"] == "IMPORTED"

    statement_txns = await db_session.execute(
        select(BankStatementTransaction).where(BankStatementTransaction.bank_account_id == account.id)
    )
    saved = list(statement_txns.scalars().all())
    assert len(saved) == 1
    assert saved[0].bank_reference == "REF-XYZ"
    assert saved[0].source_row_number == 2

    treasury_txns = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.bank_account_id == account.id)
    )
    assert list(treasury_txns.scalars().all()) == []


async def test_within_file_duplicate_rows_flagged(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="withinfiledup",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    rows = [
        _statement_row("Entity A", account.account_number, bank_reference="ABC123"),
        _statement_row("Entity A", account.account_number, bank_reference="ABC123"),
    ]
    resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    body = resp.json()
    assert body["duplicate_rows"] == 1
    error_codes = {issue["error_code"] for issue in body["issues"]}
    assert "DUPLICATE_ROW" in error_codes


async def test_reimport_of_same_statement_is_rejected_as_exact_duplicate(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from sqlalchemy import select

    from app.models.bank_statement import BankStatementTransaction
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="reimport",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    rows = [_statement_row("Entity A", account.account_number, bank_reference="REF-DUP-001")]
    first_upload = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    first_batch = first_upload.json()
    await client.post(f"/api/v1/excel/imports/{first_batch['id']}/confirm", headers=headers)

    second_upload = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    second_batch = second_upload.json()
    assert second_batch["invalid_rows"] == 1
    error_codes = {issue["error_code"] for issue in second_batch["issues"]}
    assert "DUPLICATE_TRANSACTION" in error_codes

    second_confirm = await client.post(
        f"/api/v1/excel/imports/{second_batch['id']}/confirm", headers=headers,
    )
    assert second_confirm.status_code == 400

    result = await db_session.execute(
        select(BankStatementTransaction).where(BankStatementTransaction.bank_reference == "REF-DUP-001")
    )
    assert len(list(result.scalars().all())) == 1


async def test_potential_duplicate_without_reference_is_imported_not_silently_discarded(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from sqlalchemy import select

    from app.models.bank_statement import BankStatementTransaction
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="potentialdup",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    rows_1 = [_statement_row("Entity A", account.account_number, narration="Payment 1")]
    first_upload = await _upload_and_get_batch(client, headers, entity_a.id, rows_1, make_xlsx_bytes)
    first_batch = first_upload.json()
    await client.post(f"/api/v1/excel/imports/{first_batch['id']}/confirm", headers=headers)

    rows_2 = [_statement_row("Entity A", account.account_number, narration="Payment 1")]
    second_upload = await _upload_and_get_batch(client, headers, entity_a.id, rows_2, make_xlsx_bytes)
    second_batch = second_upload.json()
    assert second_batch["valid_rows"] == 1
    warning_codes = {
        issue["error_code"] for issue in second_batch["issues"] if issue["severity"] == "WARNING"
    }
    assert "POTENTIAL_DUPLICATE" in warning_codes

    confirm_resp = await client.post(f"/api/v1/excel/imports/{second_batch['id']}/confirm", headers=headers)
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["imported_rows"] == 1

    result = await db_session.execute(
        select(BankStatementTransaction).where(BankStatementTransaction.bank_account_id == account.id)
    )
    assert len(list(result.scalars().all())) == 2


async def test_never_uses_same_date_and_amount_alone_as_duplicate_rule(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="noblinddup",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    rows = [
        _statement_row("Entity A", account.account_number, narration="Customer payment X"),
        _statement_row("Entity A", account.account_number, narration="Customer payment Y"),
    ]
    resp = await _upload_and_get_batch(client, headers, entity_a.id, rows, make_xlsx_bytes)
    body = resp.json()
    assert body["valid_rows"] == 2
    assert body["duplicate_rows"] == 0


async def test_overlapping_statement_period_is_flagged_but_not_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="periodoverlap",
    )
    await db_session.commit()
    token = await _login(client, user)
    headers = {"Authorization": f"Bearer {token}"}

    first_rows = [_statement_row("Entity A", account.account_number, bank_reference="REF-JUNE-01")]
    first_upload = await _upload_and_get_batch(client, headers, entity_a.id, first_rows, make_xlsx_bytes)
    await client.post(f"/api/v1/excel/imports/{first_upload.json()['id']}/confirm", headers=headers)

    second_rows = [_statement_row(
        "Entity A", account.account_number, txn_date="2026-06-20", bank_reference="REF-JUNE-02",
    )]
    second_upload = await _upload_and_get_batch(client, headers, entity_a.id, second_rows, make_xlsx_bytes)
    second_batch = second_upload.json()
    assert second_batch["valid_rows"] == 1
    overlap_issues = [i for i in second_batch["issues"] if i["error_code"] == "STATEMENT_PERIOD_OVERLAP"]
    assert len(overlap_issues) == 1
    assert overlap_issues[0]["severity"] == "WARNING"

    confirm_resp = await client.post(f"/api/v1/excel/imports/{second_batch['id']}/confirm", headers=headers)
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["imported_rows"] == 1
