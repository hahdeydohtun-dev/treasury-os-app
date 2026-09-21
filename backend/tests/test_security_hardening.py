"""
Stage 2 Hardening - mandatory security regression tests.

Proves, for each covered module, that an entity-scoped user can never
read, modify, export, or import another entity's (or another group's)
data, and that a properly group-scoped user's aggregate/consolidated
views only ever include their own authorized group.
"""
import datetime
import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.rbac import (
    EntityScopeType,
    Permission,
    Role,
    TreasuryAction,
    TreasuryModule,
    User,
    UserRoleAssignment,
)
from tests.conftest import make_xlsx_bytes

_MODULE_ACTIONS = [
    (TreasuryModule.FORECAST_13WK, TreasuryAction.VIEW),
    (TreasuryModule.FORECAST_13WK, TreasuryAction.CREATE),
    (TreasuryModule.FORECAST_13WK, TreasuryAction.EDIT),
    (TreasuryModule.FORECAST_13WK, TreasuryAction.ADJUST),
    (TreasuryModule.FORECAST_13WK, TreasuryAction.EXPORT),
    (TreasuryModule.FORECAST_13WK, TreasuryAction.CONFIGURE),
    (TreasuryModule.CASH_LIQUIDITY, TreasuryAction.VIEW),
    (TreasuryModule.CASH_LIQUIDITY, TreasuryAction.CREATE),
    (TreasuryModule.BANKS_ACCOUNTS, TreasuryAction.VIEW),
    (TreasuryModule.BANKS_ACCOUNTS, TreasuryAction.CREATE),
    (TreasuryModule.EXCEL_DATA_HUB, TreasuryAction.VIEW),
    (TreasuryModule.EXCEL_DATA_HUB, TreasuryAction.UPLOAD),
    (TreasuryModule.EXCEL_DATA_HUB, TreasuryAction.IMPORT),
    (TreasuryModule.GROUP_ENTITY, TreasuryAction.VIEW),
    (TreasuryModule.AUDIT, TreasuryAction.VIEW),
]


async def _make_scoped_user(
    db_session: AsyncSession, *, scope_type: EntityScopeType, legal_entity_id=None,
    group_id=None, label: str,
) -> User:
    role = Role(name=f"{label}-role-{uuid.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for module, action in _MODULE_ACTIONS:
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


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _headers(client: AsyncClient, user: User) -> dict:
    token = await _login(client, user)
    return {"Authorization": f"Bearer {token}"}


async def _make_second_group(db_session: AsyncSession):
    from app.models.entity import Group, LegalEntity

    group2 = Group(name="Second Demo Group", code="DEMOGRP2", reporting_currency_code="USD")
    db_session.add(group2)
    await db_session.flush()
    entity_c = LegalEntity(
        group_id=group2.id, name="Entity C", code="ENTC",
        functional_currency_code="USD", country="US",
    )
    db_session.add(entity_c)
    await db_session.flush()
    return group2, entity_c


async def test_forecast_list_only_returns_authorized_entity(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    from app.models.forecast import Forecast

    group, entity_a, entity_b = demo_group_and_entities
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    forecast_a = Forecast(
        group_id=group.id, legal_entity_id=entity_a.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 8, 30), reporting_currency_code="NGN",
    )
    forecast_b = Forecast(
        group_id=group.id, legal_entity_id=entity_b.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 8, 30), reporting_currency_code="USD",
    )
    db_session.add_all([forecast_a, forecast_b])
    await db_session.commit()

    headers = await _headers(client, user_a)

    resp = await client.get("/api/v1/forecast", headers=headers)
    assert resp.status_code == 200
    ids = {f["id"] for f in resp.json()}
    assert str(forecast_a.id) in ids
    assert str(forecast_b.id) not in ids

    resp = await client.get(
        "/api/v1/forecast", params={"legal_entity_id": str(entity_a.id)}, headers=headers
    )
    assert resp.status_code == 200
    assert {f["id"] for f in resp.json()} == {str(forecast_a.id)}

    resp = await client.get(
        "/api/v1/forecast", params={"legal_entity_id": str(entity_b.id)}, headers=headers
    )
    assert resp.status_code == 403

    resp = await client.get(f"/api/v1/forecast/{forecast_b.id}", headers=headers)
    assert resp.status_code == 403

    resp = await client.get(f"/api/v1/forecast/{forecast_a.id}", headers=headers)
    assert resp.status_code == 200


async def test_group_wide_user_sees_both_entities_but_not_other_groups(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    from app.models.forecast import Forecast

    group, entity_a, entity_b = demo_group_and_entities
    group2, entity_c = await _make_second_group(db_session)

    group_user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group.id, label="groupuser",
    )

    forecast_a = Forecast(
        group_id=group.id, legal_entity_id=entity_a.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 8, 30), reporting_currency_code="NGN",
    )
    forecast_b = Forecast(
        group_id=group.id, legal_entity_id=entity_b.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 8, 30), reporting_currency_code="USD",
    )
    forecast_c = Forecast(
        group_id=group2.id, legal_entity_id=entity_c.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 8, 30), reporting_currency_code="USD",
    )
    db_session.add_all([forecast_a, forecast_b, forecast_c])
    await db_session.commit()

    headers = await _headers(client, group_user)

    resp = await client.get("/api/v1/forecast", headers=headers)
    assert resp.status_code == 200
    ids = {f["id"] for f in resp.json()}
    assert str(forecast_a.id) in ids
    assert str(forecast_b.id) in ids
    assert str(forecast_c.id) not in ids

    resp = await client.get(f"/api/v1/forecast/{forecast_c.id}", headers=headers)
    assert resp.status_code == 403


async def test_user_a_cannot_create_forecast_for_entity_b(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    group, entity_a, entity_b = demo_group_and_entities
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    await db_session.commit()
    headers = await _headers(client, user_a)

    resp = await client.post(
        "/api/v1/forecast",
        json={
            "legal_entity_id": str(entity_b.id), "forecast_start_date": "2026-06-01",
            "reporting_currency_code": "USD",
        },
        headers=headers,
    )
    assert resp.status_code == 403

    resp = await client.post(
        "/api/v1/forecast",
        json={
            "legal_entity_id": str(entity_a.id), "forecast_start_date": "2026-06-01",
            "reporting_currency_code": "NGN",
        },
        headers=headers,
    )
    assert resp.status_code == 201


async def test_user_a_cannot_adjust_forecast_b(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, forecast_categories,
):
    from app.models.forecast import Forecast

    group, entity_a, entity_b = demo_group_and_entities
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    forecast_b = Forecast(
        group_id=group.id, legal_entity_id=entity_b.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 8, 30), reporting_currency_code="USD",
    )
    db_session.add(forecast_b)
    await db_session.commit()
    headers = await _headers(client, user_a)

    resp = await client.post(
        f"/api/v1/forecast/{forecast_b.id}/adjustments",
        json={
            "legal_entity_id": str(entity_b.id), "currency_code": "USD", "week_number": 1,
            "category_code": "OTHER_OUTFLOW", "amount": "100", "direction": "OUTFLOW",
            "reason": "attempted cross-entity adjustment",
        },
        headers=headers,
    )
    assert resp.status_code == 403


async def test_user_a_export_contains_only_entity_a(
    client: AsyncClient, db_session: AsyncSession, demo_bank_and_account, forecast_categories,
):
    bank, account = demo_bank_and_account
    from app.models.entity import LegalEntity
    entity_a_obj = await db_session.get(LegalEntity, account.legal_entity_id)

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a_obj.id, label="usera",
    )
    await db_session.commit()
    headers = await _headers(client, user_a)

    create_resp = await client.post(
        "/api/v1/forecast",
        json={
            "legal_entity_id": str(entity_a_obj.id), "forecast_start_date": "2026-06-01",
            "reporting_currency_code": entity_a_obj.functional_currency_code,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201
    forecast_id = create_resp.json()["id"]
    calc_resp = await client.post(f"/api/v1/forecast/{forecast_id}/calculate", headers=headers)
    assert calc_resp.status_code == 200

    export_resp = await client.get(f"/api/v1/forecast/{forecast_id}/export", headers=headers)
    assert export_resp.status_code == 200

    import io

    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(export_resp.content))
    meta = wb["Forecast Metadata"]
    entity_row = [row for row in meta.iter_rows(values_only=True) if row[0] == "Legal Entity ID"][0]
    assert entity_row[1] == str(entity_a_obj.id)


async def test_transaction_cross_entity_isolation(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.lookup import CashDirection, CashEventType
    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, entity_b = demo_group_and_entities
    if await db_session.get(CashEventType, "OTHER_INFLOW") is None:
        db_session.add(CashEventType(
            code="OTHER_INFLOW", name="Other Inflow", default_direction=CashDirection.INFLOW,
        ))
    txn_a = TreasuryTransaction(
        legal_entity_id=entity_a.id, event_type_code="OTHER_INFLOW", direction=CashDirection.INFLOW,
        event_date=datetime.date(2026, 6, 1), transaction_currency_code="NGN",
        transaction_amount=Decimal(1000),
    )
    txn_b = TreasuryTransaction(
        legal_entity_id=entity_b.id, event_type_code="OTHER_INFLOW", direction=CashDirection.INFLOW,
        event_date=datetime.date(2026, 6, 1), transaction_currency_code="USD",
        transaction_amount=Decimal(500),
    )
    db_session.add_all([txn_a, txn_b])

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    await db_session.commit()
    headers = await _headers(client, user_a)

    list_resp = await client.get("/api/v1/transactions", headers=headers)
    assert list_resp.status_code == 200
    ids = {t["id"] for t in list_resp.json()}
    assert str(txn_a.id) in ids
    assert str(txn_b.id) not in ids

    detail_resp = await client.get(f"/api/v1/transactions/{txn_b.id}", headers=headers)
    assert detail_resp.status_code == 403


async def test_bank_account_cross_entity_isolation(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.banking import Bank, BankAccount
    from app.models.lookup import AccountType

    group, entity_a, entity_b = demo_group_and_entities
    if await db_session.get(AccountType, "CURRENT") is None:
        db_session.add(AccountType(code="CURRENT", name="Current Account"))
    bank = Bank(name="Cross Entity Test Bank")
    db_session.add(bank)
    await db_session.flush()

    account_a = BankAccount(
        legal_entity_id=entity_a.id, bank_id=bank.id, account_name="A Account",
        account_number="AAA111", currency_code="NGN", account_type_code="CURRENT",
    )
    account_b = BankAccount(
        legal_entity_id=entity_b.id, bank_id=bank.id, account_name="B Account",
        account_number="BBB222", currency_code="USD", account_type_code="CURRENT",
    )
    db_session.add_all([account_a, account_b])

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    await db_session.commit()
    headers = await _headers(client, user_a)

    list_resp = await client.get("/api/v1/bank-accounts", headers=headers)
    assert list_resp.status_code == 200
    ids = {a["id"] for a in list_resp.json()}
    assert str(account_a.id) in ids
    assert str(account_b.id) not in ids

    detail_resp = await client.get(f"/api/v1/bank-accounts/{account_b.id}", headers=headers)
    assert detail_resp.status_code == 403


async def test_cash_position_aggregate_excludes_unauthorized_entity(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.balance import BankBalance
    from app.models.banking import Bank, BankAccount
    from app.models.lookup import AccountType

    group, entity_a, entity_b = demo_group_and_entities
    if await db_session.get(AccountType, "CURRENT") is None:
        db_session.add(AccountType(code="CURRENT", name="Current Account"))
    bank = Bank(name="Cash Position Test Bank")
    db_session.add(bank)
    await db_session.flush()

    account_a = BankAccount(
        legal_entity_id=entity_a.id, bank_id=bank.id, account_name="A Account",
        account_number="CPA001", currency_code="NGN", account_type_code="CURRENT",
    )
    account_b = BankAccount(
        legal_entity_id=entity_b.id, bank_id=bank.id, account_name="B Account",
        account_number="CPB002", currency_code="NGN", account_type_code="CURRENT",
    )
    db_session.add_all([account_a, account_b])
    await db_session.flush()

    db_session.add_all([
        BankBalance(
            bank_account_id=account_a.id, balance_date=datetime.date(2026, 6, 1),
            currency_code="NGN", closing_balance=Decimal(1000000), source="MANUAL",
        ),
        BankBalance(
            bank_account_id=account_b.id, balance_date=datetime.date(2026, 6, 1),
            currency_code="NGN", closing_balance=Decimal(5000000), source="MANUAL",
        ),
    ])

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    await db_session.commit()
    headers = await _headers(client, user_a)

    resp = await client.get(
        "/api/v1/cash-position", params={"as_of": "2026-06-05"}, headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cash"] == "1000000.00"
    assert body["account_count"] == 1


async def test_expected_collection_and_payment_cross_entity_isolation(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.expected_cash_flow import ExpectedCollection, ExpectedPayment

    group, entity_a, entity_b = demo_group_and_entities
    coll_a = ExpectedCollection(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 1),
        currency_code="NGN", amount=Decimal(1000),
    )
    coll_b = ExpectedCollection(
        legal_entity_id=entity_b.id, expected_date=datetime.date(2026, 6, 1),
        currency_code="USD", amount=Decimal(2000),
    )
    pay_a = ExpectedPayment(
        legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 6, 1),
        currency_code="NGN", amount=Decimal(500),
    )
    pay_b = ExpectedPayment(
        legal_entity_id=entity_b.id, expected_date=datetime.date(2026, 6, 1),
        currency_code="USD", amount=Decimal(700),
    )
    db_session.add_all([coll_a, coll_b, pay_a, pay_b])

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    await db_session.commit()
    headers = await _headers(client, user_a)

    coll_list = await client.get("/api/v1/expected-collections", headers=headers)
    assert coll_list.status_code == 200
    amounts = {c["amount"] for c in coll_list.json()}
    assert "1000.00" in amounts
    assert "2000.00" not in amounts

    coll_detail_b = await client.get(f"/api/v1/expected-collections/{coll_b.id}", headers=headers)
    assert coll_detail_b.status_code == 403

    pay_list = await client.get("/api/v1/expected-payments", headers=headers)
    assert pay_list.status_code == 200
    pay_amounts = {p["amount"] for p in pay_list.json()}
    assert "500.00" in pay_amounts
    assert "700.00" not in pay_amounts

    pay_detail_b = await client.get(f"/api/v1/expected-payments/{pay_b.id}", headers=headers)
    assert pay_detail_b.status_code == 403


async def test_recurring_cash_flow_and_liquidity_threshold_isolation(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.forecast import (
        LiquidityScopeType,
        LiquidityThreshold,
        RecurringCashFlow,
        RecurringFrequency,
    )
    from app.models.lookup import CashDirection

    group, entity_a, entity_b = demo_group_and_entities
    flow_a = RecurringCashFlow(
        name="A payroll", legal_entity_id=entity_a.id, currency_code="NGN",
        category_code="PAYROLL", amount=Decimal(100000), direction=CashDirection.OUTFLOW,
        start_date=datetime.date(2026, 6, 1), frequency=RecurringFrequency.MONTHLY,
    )
    flow_b = RecurringCashFlow(
        name="B payroll", legal_entity_id=entity_b.id, currency_code="USD",
        category_code="PAYROLL", amount=Decimal(50000), direction=CashDirection.OUTFLOW,
        start_date=datetime.date(2026, 6, 1), frequency=RecurringFrequency.MONTHLY,
    )
    threshold_b = LiquidityThreshold(
        scope_type=LiquidityScopeType.ENTITY, legal_entity_id=entity_b.id,
        minimum_amount=Decimal(999999),
    )
    db_session.add_all([flow_a, flow_b, threshold_b])

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    await db_session.commit()
    headers = await _headers(client, user_a)

    flows_resp = await client.get("/api/v1/forecast/admin/recurring-cash-flows", headers=headers)
    assert flows_resp.status_code == 200
    names = {f["name"] for f in flows_resp.json()}
    assert "A payroll" in names
    assert "B payroll" not in names

    thresholds_resp = await client.get("/api/v1/forecast/admin/liquidity-thresholds", headers=headers)
    assert thresholds_resp.status_code == 200
    entity_ids_seen = {t.get("legal_entity_id") for t in thresholds_resp.json()}
    assert str(entity_b.id) not in entity_ids_seen


async def test_excel_import_rejects_unauthorized_entity_rows(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    from app.models.banking import Bank

    group, entity_a, entity_b = demo_group_and_entities
    bank = Bank(name="Excel Import Test Bank")
    db_session.add(bank)

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    await db_session.commit()
    headers = await _headers(client, user_a)

    file_bytes = make_xlsx_bytes(
        ["Entity", "Bank", "Account Name", "Account Number", "Currency"],
        [
            ["Entity A", "Excel Import Test Bank", "Legit A Account", "IMPA001", "NGN"],
            ["Entity B", "Excel Import Test Bank", "Smuggled B Account", "IMPB002", "USD"],
        ],
    )
    upload_resp = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "BANK_ACCOUNTS", "template_version": "1",
              "legal_entity_id": str(entity_a.id)},
        files={"file": ("accounts.xlsx", file_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )
    assert upload_resp.status_code == 201
    batch = upload_resp.json()
    assert batch["valid_rows"] == 2

    confirm_resp = await client.post(
        f"/api/v1/excel/imports/{batch['id']}/confirm", headers=headers
    )
    assert confirm_resp.status_code == 200
    result = confirm_resp.json()
    assert result["imported_rows"] == 1
    assert result["skipped_rows"] == 1

    accounts_resp = await client.get(
        "/api/v1/bank-accounts", params={"legal_entity_id": str(entity_a.id)}, headers=headers,
    )
    names = {a["account_name"] for a in accounts_resp.json()}
    assert "Legit A Account" in names
    assert "Smuggled B Account" not in names


async def test_excel_import_history_and_validation_detail_are_entity_scoped(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    group, entity_a, entity_b = demo_group_and_entities
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    user_b = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_b.id, label="userb",
    )
    await db_session.commit()

    headers_a = await _headers(client, user_a)
    headers_b = await _headers(client, user_b)

    file_bytes = make_xlsx_bytes(
        ["Rate Date", "From Currency", "To Currency", "Rate", "Rate Type", "Source"],
        [["2026-06-01", "USD", "NGN", "1500", "SPOT", "TEST"]],
    )
    upload_resp = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "FX_RATES", "template_version": "1",
              "legal_entity_id": str(entity_a.id)},
        files={"file": ("fx.xlsx", file_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers_a,
    )
    assert upload_resp.status_code == 201
    batch_id = upload_resp.json()["id"]

    detail_resp = await client.get(f"/api/v1/excel/validation/{batch_id}", headers=headers_b)
    assert detail_resp.status_code == 403

    history_resp = await client.get("/api/v1/excel/imports", headers=headers_b)
    assert history_resp.status_code == 200
    assert batch_id not in {b["id"] for b in history_resp.json()}

    own_detail = await client.get(f"/api/v1/excel/validation/{batch_id}", headers=headers_a)
    assert own_detail.status_code == 200
