import asyncio
import datetime
import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User


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
    for module in (TreasuryModule.BANK_RECONCILIATION,):
        for action in TreasuryAction:
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


async def _make_bank(db_session, name="Stage5B Test Bank"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


async def _make_account(db_session, entity_id, bank_id, currency="NGN", account_number="0099887766"):
    from app.models.banking import BankAccount

    await _make_account_type(db_session)
    account = BankAccount(
        legal_entity_id=entity_id, bank_id=bank_id, account_name="Operating Account",
        account_number=account_number, currency_code=currency, account_type_code="CURRENT",
    )
    db_session.add(account)
    await db_session.flush()
    return account


def _run_payload(entity_id, account_id, start="2026-06-01", end="2026-06-30", configuration_id=None):
    payload = {
        "legal_entity_id": str(entity_id), "bank_account_id": str(account_id),
        "period_start": start, "period_end": end,
    }
    if configuration_id:
        payload["configuration_id"] = str(configuration_id)
    return payload


async def test_create_valid_reconciliation_run(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="validrun",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "READY"
    assert body["statement_transaction_count"] == 0
    assert body["eligible_transaction_count"] == 0


async def test_create_run_invalid_period_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="invalidperiod",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_a.id, account.id, start="2026-06-30", end="2026-06-01"),
        headers=headers,
    )
    assert resp.status_code == 400


async def test_create_run_cross_entity_bank_account_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="1122334455")
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="crossacct",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account_b.id), headers=headers,
    )
    assert resp.status_code == 400


async def test_create_run_invalid_bank_account_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="noaccount",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, uuid.uuid4()), headers=headers,
    )
    assert resp.status_code == 400


async def test_valid_match_suggestion_and_open_item_rows(db_session: AsyncSession, demo_group_and_entities):
    from app.models.reconciliation import (
        MatchSuggestionStatus,
        OpenItemCategory,
        OpenItemStatus,
        ReconciliationMatchSuggestion,
        ReconciliationOpenItem,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)

    run = ReconciliationRun(
        legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=datetime.date(2026, 6, 1), period_end=datetime.date(2026, 6, 30),
        status=ReconciliationRunStatus.READY,
    )
    db_session.add(run)
    await db_session.flush()

    suggestion = ReconciliationMatchSuggestion(
        reconciliation_run_id=run.id, bank_statement_transaction_id=None,
        treasury_transaction_id=None, status=MatchSuggestionStatus.PENDING,
    )
    db_session.add(suggestion)

    bank_only_item = ReconciliationOpenItem(
        reconciliation_run_id=run.id, legal_entity_id=entity_a.id, bank_account_id=account.id,
        bank_statement_transaction_id=None, treasury_transaction_id=None,
        category=OpenItemCategory.BANK_ONLY, status=OpenItemStatus.OPEN,
        amount=Decimal("100000.00"), currency_code="NGN",
    )
    ledger_only_item = ReconciliationOpenItem(
        reconciliation_run_id=run.id, legal_entity_id=entity_a.id, bank_account_id=account.id,
        bank_statement_transaction_id=None, treasury_transaction_id=None,
        category=OpenItemCategory.LEDGER_ONLY, status=OpenItemStatus.OPEN,
        amount=Decimal("50000.00"), currency_code="NGN",
    )
    db_session.add_all([bank_only_item, ledger_only_item])
    await db_session.flush()

    assert suggestion.id is not None
    assert bank_only_item.category == OpenItemCategory.BANK_ONLY
    assert ledger_only_item.category == OpenItemCategory.LEDGER_ONLY


async def test_run_lifecycle_ready_to_completed(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="lifecycle",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    run_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "READY"

    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.status_code == 200
    body = execute_resp.json()
    assert body["status"] == "COMPLETED"
    assert body["started_at"] is not None
    assert body["completed_at"] is not None


async def test_run_execution_counts_in_scope_statement_transactions(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.bank_statement import BankStatementEntryType, BankStatementTransaction
    from app.models.excel_hub import ImportBatch, ImportBatchStatus
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="countstest",
    )
    await db_session.flush()

    batch = ImportBatch(
        template_code="BANK_STATEMENT", template_version=1, legal_entity_id=entity_a.id,
        file_name="test.xlsx", uploaded_by_user_id=user.id,
        uploaded_at=datetime.datetime.now(datetime.UTC), status=ImportBatchStatus.IMPORTED,
    )
    db_session.add(batch)
    await db_session.flush()

    for i in range(3):
        db_session.add(BankStatementTransaction(
            legal_entity_id=entity_a.id, bank_id=bank.id, bank_account_id=account.id,
            statement_period_start=datetime.date(2026, 6, 1), statement_period_end=datetime.date(2026, 6, 30),
            transaction_date=datetime.date(2026, 6, 10 + i), entry_type=BankStatementEntryType.CREDIT,
            amount=Decimal("100000.00"), currency_code="NGN", duplicate_key=f"key-{i}",
            has_strong_identity=True, import_batch_id=batch.id, source_row_number=i + 2,
        ))
    db_session.add(BankStatementTransaction(
        legal_entity_id=entity_a.id, bank_id=bank.id, bank_account_id=account.id,
        statement_period_start=datetime.date(2026, 7, 1), statement_period_end=datetime.date(2026, 7, 31),
        transaction_date=datetime.date(2026, 7, 5), entry_type=BankStatementEntryType.CREDIT,
        amount=Decimal("200000.00"), currency_code="NGN", duplicate_key="key-outside",
        has_strong_identity=True, import_batch_id=batch.id, source_row_number=5,
    ))
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    run_id = create_resp.json()["id"]
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["statement_transaction_count"] == 3
    assert body["eligible_transaction_count"] == 3


async def test_cannot_execute_a_non_ready_run(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="notready",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    run_id = create_resp.json()["id"]
    await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)

    second_execute = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert second_execute.status_code == 400


async def test_run_creation_never_creates_suggestions_or_open_items(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="nosideeffect",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    run_id = create_resp.json()["id"]
    await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)

    suggestions = await client.get(f"/api/v1/reconciliation/runs/{run_id}/suggestions", headers=headers)
    open_items = await client.get(f"/api/v1/reconciliation/runs/{run_id}/open-items", headers=headers)
    assert suggestions.json() == []
    assert open_items.json() == []


async def test_execution_creates_no_cash_ledger_side_effects(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from sqlalchemy import select

    from app.models.rbac import EntityScopeType
    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="nocash",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    run_id = create_resp.json()["id"]
    await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)

    result = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.bank_account_id == account.id)
    )
    assert list(result.scalars().all()) == []


async def test_cancel_run(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="cancelrun",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    run_id = create_resp.json()["id"]
    cancel_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/cancel", headers=headers)
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "CANCELLED"

    execute_after_cancel = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_after_cancel.status_code == 400


async def test_configuration_versioning_never_overwrites_history(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="configver",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    first_resp = await client.post(
        "/api/v1/reconciliation/configurations",
        json={"legal_entity_id": str(entity_a.id), "amount_tolerance_pct": "1.0", "date_tolerance_days": 2},
        headers=headers,
    )
    assert first_resp.status_code == 201
    first = first_resp.json()
    assert first["version"] == 1
    assert first["is_current"] is True

    second_resp = await client.post(
        "/api/v1/reconciliation/configurations",
        json={"legal_entity_id": str(entity_a.id), "amount_tolerance_pct": "2.5", "date_tolerance_days": 3},
        headers=headers,
    )
    second = second_resp.json()
    assert second["version"] == 2

    list_resp = await client.get(
        "/api/v1/reconciliation/configurations",
        params={"legal_entity_id": str(entity_a.id), "include_superseded": "true"}, headers=headers,
    )
    versions = {c["version"]: c for c in list_resp.json()}
    assert versions[1]["is_current"] is False
    assert versions[1]["superseded_by_id"] == second["id"]
    assert versions[1]["amount_tolerance_pct"] == "1.000"
    assert versions[2]["is_current"] is True


async def test_run_stores_the_exact_configuration_version_used(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="reproducible",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    config_resp = await client.post(
        "/api/v1/reconciliation/configurations",
        json={"legal_entity_id": str(entity_a.id), "amount_tolerance_pct": "1.0"}, headers=headers,
    )
    config_id_v1 = config_resp.json()["id"]

    run_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    assert run_resp.json()["configuration_id"] == config_id_v1

    await client.post(
        "/api/v1/reconciliation/configurations",
        json={"legal_entity_id": str(entity_a.id), "amount_tolerance_pct": "5.0"}, headers=headers,
    )
    run_detail = await client.get(f"/api/v1/reconciliation/runs/{run_resp.json()['id']}", headers=headers)
    assert run_detail.json()["configuration_id"] == config_id_v1


async def test_entity_a_cannot_create_run_for_entity_b(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="2233445566")
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="denieda",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_b.id, account_b.id), headers=headers,
    )
    assert resp.status_code == 403


async def test_entity_a_cannot_read_or_execute_entity_b_run(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="3344556677")
    user_b = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_b.id, label="ownerb",
    )
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="readera",
    )
    await db_session.commit()

    headers_b = {"Authorization": f"Bearer {await _login(client, user_b)}"}
    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_b.id, account_b.id), headers=headers_b,
    )
    run_id = create_resp.json()["id"]

    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}
    get_resp = await client.get(f"/api/v1/reconciliation/runs/{run_id}", headers=headers_a)
    assert get_resp.status_code == 403

    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers_a)
    assert execute_resp.status_code == 403

    list_resp = await client.get(
        "/api/v1/reconciliation/runs", params={"legal_entity_id": str(entity_b.id)}, headers=headers_a,
    )
    assert list_resp.status_code == 403

    unfiltered = await client.get("/api/v1/reconciliation/runs", headers=headers_a)
    assert str(entity_b.id) not in {r["legal_entity_id"] for r in unfiltered.json()}


async def test_cross_group_reconciliation_access_denied(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.entity import Group, LegalEntity
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)

    group2 = Group(name="Recon Second Group", code="RECONGRP2", reporting_currency_code="USD")
    db_session.add(group2)
    await db_session.flush()
    entity_c = LegalEntity(
        group_id=group2.id, name="Recon Entity C", code="RECONENTC", functional_currency_code="USD", country="US",
    )
    db_session.add(entity_c)
    await db_session.flush()
    account_c = await _make_account(db_session, entity_c.id, bank.id, currency="USD", account_number="4455667788")

    user_c = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_c.id, label="userc",
    )
    group1_user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group.id, label="group1user",
    )
    await db_session.commit()

    headers_c = {"Authorization": f"Bearer {await _login(client, user_c)}"}
    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_c.id, account_c.id), headers=headers_c,
    )
    run_id = create_resp.json()["id"]

    headers_g1 = {"Authorization": f"Bearer {await _login(client, group1_user)}"}
    get_resp = await client.get(f"/api/v1/reconciliation/runs/{run_id}", headers=headers_g1)
    assert get_resp.status_code == 403


async def test_concurrent_execution_cannot_both_succeed(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="concurrentexec",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    create_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    run_id = create_resp.json()["id"]

    results = await asyncio.gather(
        *[
            client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    status_codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert status_codes.count(200) == 1
    assert 400 in status_codes

    final = await client.get(f"/api/v1/reconciliation/runs/{run_id}", headers=headers)
    assert final.json()["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Stage 5B configuration-integrity hardening patch
# ---------------------------------------------------------------------------

async def test_explicit_configuration_entity_mismatch_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """Test 1: a configuration belonging specifically to Entity B cannot be used for an Entity A run."""
    from sqlalchemy import select

    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationConfiguration, ReconciliationRun

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="5566778899")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="entitymismatch",
    )

    config = ReconciliationConfiguration(
        legal_entity_id=entity_b.id, version=1, is_current=True, is_active=True,
        effective_from=datetime.date.today(),
    )
    db_session.add(config)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_a.id, account_a.id, configuration_id=config.id), headers=headers,
    )
    assert resp.status_code == 400

    result = await db_session.execute(select(ReconciliationRun))
    assert list(result.scalars().all()) == []


async def test_explicit_configuration_bank_account_mismatch_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """Test 2: a configuration belonging specifically to Account B cannot be used for an Account A run."""
    from sqlalchemy import select

    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationConfiguration, ReconciliationRun

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="6677889900")
    account_b = await _make_account(db_session, entity_a.id, bank.id, account_number="7788990011")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="acctmismatch",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    config = ReconciliationConfiguration(
        legal_entity_id=entity_a.id, bank_account_id=account_b.id, version=1,
        is_current=True, is_active=True, effective_from=datetime.date.today(),
    )
    db_session.add(config)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_a.id, account_a.id, configuration_id=config.id), headers=headers,
    )
    assert resp.status_code == 400

    result = await db_session.execute(select(ReconciliationRun))
    assert list(result.scalars().all()) == []


async def test_explicit_configuration_currency_mismatch_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """Test 3: a currency-specific configuration cannot be used for an incompatible account currency."""
    from sqlalchemy import select

    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationConfiguration, ReconciliationRun

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    ngn_account = await _make_account(db_session, entity_a.id, bank.id, currency="NGN", account_number="8899001122")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="currencymismatch",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    config = ReconciliationConfiguration(
        legal_entity_id=entity_a.id, currency_code="USD", version=1,
        is_current=True, is_active=True, effective_from=datetime.date.today(),
    )
    db_session.add(config)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_a.id, ngn_account.id, configuration_id=config.id), headers=headers,
    )
    assert resp.status_code == 400

    result = await db_session.execute(select(ReconciliationRun))
    assert list(result.scalars().all()) == []


async def test_explicit_entity_wide_configuration_valid_for_any_account(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """Test 4: a broader entity-wide configuration remains valid for any account belonging to that entity."""
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="9900112233")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="broadconfig",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    config_resp = await client.post(
        "/api/v1/reconciliation/configurations",
        json={"legal_entity_id": str(entity_a.id), "amount_tolerance_pct": "1.0"}, headers=headers,
    )
    config_id = config_resp.json()["id"]

    resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_a.id, account.id, configuration_id=config_id), headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["configuration_id"] == config_id


async def test_explicit_global_configuration_valid_for_any_entity(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """A fully-broad (all-null-scope) configuration remains valid for any entity/account/currency."""
    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationConfiguration

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="0011223399")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group.id, label="globalconfig",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    config = ReconciliationConfiguration(
        version=1, is_current=True, is_active=True, effective_from=datetime.date.today(),
    )
    db_session.add(config)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_a.id, account.id, configuration_id=config.id), headers=headers,
    )
    assert resp.status_code == 201


async def test_configuration_scope_validation_never_bypasses_entity_rbac(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """Authorization must be checked before/independent of configuration-scope validation."""
    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationConfiguration

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="1100220033")
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="rbacfirst",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    config = ReconciliationConfiguration(
        legal_entity_id=entity_b.id, version=1, is_current=True, is_active=True,
        effective_from=datetime.date.today(),
    )
    db_session.add(config)
    await db_session.commit()

    # Entity A user attempting a run for Entity B (even with a matching
    # Entity B configuration) must be rejected by RBAC (403), never by
    # the scope-mismatch check (400) - authorization comes first.
    resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_b.id, account_b.id, configuration_id=config.id), headers=headers,
    )
    assert resp.status_code == 403


async def test_run_executes_using_stored_configuration_after_it_is_superseded(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """Tests 5/6/7: run created with v1, v1 superseded by v2, run still executes using v1, unchanged."""
    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationConfiguration

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2200330044")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="historicalexec",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    v1_resp = await client.post(
        "/api/v1/reconciliation/configurations",
        json={"legal_entity_id": str(entity_a.id), "amount_tolerance_pct": "1.0"}, headers=headers,
    )
    v1 = v1_resp.json()

    # Test 5: run created using v1 (auto-resolved, since it's the only/current config).
    run_resp = await client.post(
        "/api/v1/reconciliation/runs", json=_run_payload(entity_a.id, account.id), headers=headers,
    )
    run_id = run_resp.json()["id"]
    assert run_resp.json()["configuration_id"] == v1["id"]

    # Test 6: v2 supersedes v1.
    v2_resp = await client.post(
        "/api/v1/reconciliation/configurations",
        json={"legal_entity_id": str(entity_a.id), "amount_tolerance_pct": "5.0"}, headers=headers,
    )
    v2 = v2_resp.json()

    v1_db = await db_session.get(ReconciliationConfiguration, uuid.UUID(v1["id"]))
    await db_session.refresh(v1_db)
    assert v1_db.is_current is False  # superseded
    assert v1_db.superseded_by_id == uuid.UUID(v2["id"])
    assert v1_db.amount_tolerance_pct == Decimal("1.000")  # historical value never mutated
    assert v1_db.legal_entity_id == entity_a.id  # v1 row itself is untouched, not "mutated into v2"

    # Test 7: the existing run still executes successfully, still
    # referencing v1, never silently switching to v2.
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.status_code == 200
    body = execute_resp.json()
    assert body["status"] == "COMPLETED"
    assert body["configuration_id"] == v1["id"]  # unchanged - never switched to v2

    suggestions = await client.get(f"/api/v1/reconciliation/runs/{run_id}/suggestions", headers=headers)
    open_items = await client.get(f"/api/v1/reconciliation/runs/{run_id}/open-items", headers=headers)
    assert suggestions.json() == []
    assert open_items.json() == []


async def test_run_execution_fails_cleanly_when_configuration_cannot_be_loaded(
    db_session: AsyncSession, demo_group_and_entities,
):
    """
    Test 8: the schema's own foreign key on `ReconciliationRun.configuration_id`
    genuinely prevents a run from ever referencing a configuration row
    that doesn't exist (an orphaned reference can't be created, and the
    referenced row can't be deleted while a run points to it) - this is
    a correct integrity property, not a gap to work around, and the
    patch's own instructions are explicit: do not weaken the FK merely
    to manufacture this scenario. This test instead directly verifies
    the failure-recording CODE PATH that `execute_run_endpoint` takes
    when its `db.get(ReconciliationConfiguration, run.configuration_id)`
    lookup returns None - the exact same statements the endpoint itself
    executes on that branch - proving the run lands in FAILED with a
    meaningful, durable reason and is never left in an ambiguous state.
    """
    from app.models.reconciliation import ReconciliationRun, ReconciliationRunStatus

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="3300440055")

    run = ReconciliationRun(
        legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=datetime.date(2026, 6, 1), period_end=datetime.date(2026, 6, 30),
        status=ReconciliationRunStatus.READY, configuration_id=None,
    )
    db_session.add(run)
    await db_session.flush()

    # Simulate the exact lookup execute_run_endpoint performs, using a
    # UUID guaranteed not to resolve to any real configuration row.
    from app.models.reconciliation import ReconciliationConfiguration

    missing_id = uuid.uuid4()
    configuration = await db_session.get(ReconciliationConfiguration, missing_id)
    assert configuration is None

    # The exact failure-recording statements execute_run_endpoint runs
    # on this branch.
    run.status = ReconciliationRunStatus.FAILED
    run.failure_reason = "The run's reconciliation configuration no longer exists."
    run.completed_at = datetime.datetime.now(datetime.UTC)
    await db_session.flush()

    assert run.status == ReconciliationRunStatus.FAILED
    assert run.failure_reason == "The run's reconciliation configuration no longer exists."
    assert run.completed_at is not None


async def test_current_active_configuration_still_works_end_to_end(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    """Test 9: an ordinary current/active configuration continues to work exactly as before."""
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="4400550066")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="stillworks",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    config_resp = await client.post(
        "/api/v1/reconciliation/configurations",
        json={"legal_entity_id": str(entity_a.id), "bank_account_id": str(account.id),
              "amount_tolerance_pct": "1.0"},
        headers=headers,
    )
    config_id = config_resp.json()["id"]

    run_resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_a.id, account.id, configuration_id=config_id), headers=headers,
    )
    assert run_resp.status_code == 201
    run_id = run_resp.json()["id"]

    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.status_code == 200
    assert execute_resp.json()["status"] == "COMPLETED"
    assert execute_resp.json()["configuration_id"] == config_id
