from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_create_bank_account_masks_account_number(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities,
):
    _, entity_a, _ = demo_group_and_entities
    from app.models.banking import Bank
    from app.models.lookup import AccountType

    if await db_session.get(AccountType, "CURRENT") is None:
        db_session.add(AccountType(code="CURRENT", name="Current Account"))
    bank = Bank(name="Test Bank")
    db_session.add(bank)
    await db_session.flush()
    await db_session.commit()

    token = await _login(client, group_wide_manager)
    resp = await client.post(
        "/api/v1/bank-accounts",
        json={
            "legal_entity_id": str(entity_a.id),
            "bank_id": str(bank.id),
            "account_name": "Main Account",
            "account_number": "1234567890",
            "currency_code": "NGN",
            "account_type_code": "CURRENT",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["account_number_masked"] == "******7890"
    assert "1234567890" not in resp.text


async def test_entity_scoped_user_cannot_create_account_for_other_entity(
    client: AsyncClient, db_session: AsyncSession, entity_a_scoped_user: User,
    demo_group_and_entities,
):
    _, entity_a, entity_b = demo_group_and_entities
    from app.models.banking import Bank
    from app.models.lookup import AccountType

    if await db_session.get(AccountType, "CURRENT") is None:
        db_session.add(AccountType(code="CURRENT", name="Current Account"))
    bank = Bank(name="Test Bank")
    db_session.add(bank)
    await db_session.flush()
    await db_session.commit()

    token = await _login(client, entity_a_scoped_user)
    headers = {"Authorization": f"Bearer {token}"}

    # Allowed: Entity A
    resp_a = await client.post(
        "/api/v1/bank-accounts",
        json={
            "legal_entity_id": str(entity_a.id), "bank_id": str(bank.id),
            "account_name": "A Account", "account_number": "111", "currency_code": "NGN",
            "account_type_code": "CURRENT",
        },
        headers=headers,
    )
    assert resp_a.status_code == 201

    # Denied: Entity B
    resp_b = await client.post(
        "/api/v1/bank-accounts",
        json={
            "legal_entity_id": str(entity_b.id), "bank_id": str(bank.id),
            "account_name": "B Account", "account_number": "222", "currency_code": "USD",
            "account_type_code": "CURRENT",
        },
        headers=headers,
    )
    assert resp_b.status_code == 403


async def test_bank_account_create_requires_auth(client: AsyncClient):
    resp = await client.post("/api/v1/bank-accounts", json={})
    assert resp.status_code == 401
