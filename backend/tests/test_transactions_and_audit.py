from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lookup import CashDirection, CashEventType
from app.models.rbac import User


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_transaction_defaults_direction_from_event_type(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities,
):
    _, entity_a, _ = demo_group_and_entities
    if await db_session.get(CashEventType, "SUPPLIER_PAYMENT") is None:
        db_session.add(CashEventType(
            code="SUPPLIER_PAYMENT", name="Supplier Payment", default_direction=CashDirection.OUTFLOW
        ))
    await db_session.commit()

    token = await _login(client, group_wide_manager)
    resp = await client.post(
        "/api/v1/transactions",
        json={
            "legal_entity_id": str(entity_a.id),
            "event_type_code": "SUPPLIER_PAYMENT",
            "event_date": "2026-01-15",
            "transaction_currency_code": "NGN",
            "transaction_amount": "50000",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert resp.json()["direction"] == "OUTFLOW"


async def test_transaction_create_is_audited(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities,
):
    from app.models.audit import AuditEvent

    _, entity_a, _ = demo_group_and_entities
    if await db_session.get(CashEventType, "BANK_RECEIPT") is None:
        db_session.add(CashEventType(
            code="BANK_RECEIPT", name="Bank Receipt", default_direction=CashDirection.INFLOW
        ))
    await db_session.commit()

    token = await _login(client, group_wide_manager)
    resp = await client.post(
        "/api/v1/transactions",
        json={
            "legal_entity_id": str(entity_a.id),
            "event_type_code": "BANK_RECEIPT",
            "event_date": "2026-01-15",
            "transaction_currency_code": "NGN",
            "transaction_amount": "12345",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    txn_id = resp.json()["id"]

    result = await db_session.execute(
        select(AuditEvent).where(
            AuditEvent.record_type == "TreasuryTransaction", AuditEvent.record_id == txn_id
        )
    )
    events = result.scalars().all()
    assert len(events) == 1
    assert events[0].action == "CREATE"
    assert events[0].module == "CASH_LIQUIDITY"


async def test_entity_scoped_user_cannot_create_transaction_for_other_entity(
    client: AsyncClient, db_session: AsyncSession, entity_a_scoped_user: User,
    demo_group_and_entities,
):
    _, entity_a, entity_b = demo_group_and_entities
    if await db_session.get(CashEventType, "OTHER_OUTFLOW") is None:
        db_session.add(CashEventType(
            code="OTHER_OUTFLOW", name="Other Outflow", default_direction=CashDirection.OUTFLOW
        ))
    await db_session.commit()

    token = await _login(client, entity_a_scoped_user)
    resp = await client.post(
        "/api/v1/transactions",
        json={
            "legal_entity_id": str(entity_b.id),
            "event_type_code": "OTHER_OUTFLOW",
            "event_date": "2026-01-15",
            "transaction_currency_code": "USD",
            "transaction_amount": "1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_expected_collection_and_payment_and_bank_charge_create(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_bank_and_account,
):
    bank, account = demo_bank_and_account
    await db_session.commit()

    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    collection_resp = await client.post(
        "/api/v1/expected-collections",
        json={
            "legal_entity_id": str(account.legal_entity_id),
            "expected_date": "2026-02-01", "currency_code": "NGN", "amount": "75000",
            "counterparty": "Big Customer Ltd", "probability": 80,
        },
        headers=headers,
    )
    assert collection_resp.status_code == 201

    payment_resp = await client.post(
        "/api/v1/expected-payments",
        json={
            "legal_entity_id": str(account.legal_entity_id),
            "expected_date": "2026-02-05", "currency_code": "NGN", "amount": "30000",
            "counterparty": "Key Supplier Ltd", "priority": "HIGH",
        },
        headers=headers,
    )
    assert payment_resp.status_code == 201

    charge_resp = await client.post(
        "/api/v1/bank-charges",
        json={
            "legal_entity_id": str(account.legal_entity_id),
            "bank_id": str(bank.id), "bank_account_id": str(account.id),
            "charge_date": "2026-02-01", "currency_code": "NGN", "amount": "500",
            "charge_type": "SWIFT_FEE",
        },
        headers=headers,
    )
    assert charge_resp.status_code == 201

    list_resp = await client.get(
        "/api/v1/expected-collections",
        params={"legal_entity_id": str(account.legal_entity_id)}, headers=headers,
    )
    assert len(list_resp.json()) == 1
