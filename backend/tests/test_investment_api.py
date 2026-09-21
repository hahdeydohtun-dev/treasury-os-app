
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _make_bank(db_session, name="Investment Test Bank"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


def _investment_payload(entity_id, bank_id, **overrides):
    payload = {
        "investment_reference": "INV-API-001", "investment_type_code": "FIXED_DEPOSIT",
        "legal_entity_id": str(entity_id), "institution_id": str(bank_id), "currency_code": "NGN",
        "principal_amount": "300000000", "start_date": "2026-06-01", "maturity_date": "2026-09-01",
        "interest_rate": "18.0", "day_count_convention": "ACT_365",
    }
    payload.update(overrides)
    return payload


async def _create_and_approve(client, headers, entity_id, bank_id, **overrides):
    resp = await client.post(
        "/api/v1/investments", json=_investment_payload(entity_id, bank_id, **overrides), headers=headers,
    )
    assert resp.status_code == 201
    inv_id = resp.json()["id"]
    for new_status in ("SUBMITTED", "UNDER_REVIEW", "APPROVED", "PLACEMENT_PENDING"):
        r = await client.patch(f"/api/v1/investments/{inv_id}/status", json={"new_status": new_status}, headers=headers)
        assert r.status_code == 200, r.text
    return inv_id


async def test_create_investment_expected_interest_calculated(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/investments", json=_investment_payload(entity_a.id, bank.id), headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["expected_interest"] == "13610958.90"


async def test_investment_lifecycle_illegal_transition_rejected(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/investments", json=_investment_payload(entity_a.id, bank.id), headers=headers,
    )
    inv_id = resp.json()["id"]

    bad = await client.patch(f"/api/v1/investments/{inv_id}/status", json={"new_status": "ACTIVE"}, headers=headers)
    assert bad.status_code == 400


async def test_placement_is_idempotent(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id)

    first = await client.post(
        f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["status"] == "ACTIVE"

    second = await client.post(
        f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert second.status_code == 400

    txns = await client.get(f"/api/v1/investments/{inv_id}/transactions", headers=headers)
    placements = [t for t in txns.json() if t["transaction_type"] == "PLACEMENT"]
    assert len(placements) == 1


async def test_partial_then_full_termination(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id)
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    partial = await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "100000000", "termination_date": "2026-07-01"}, headers=headers,
    )
    assert partial.status_code == 200
    assert partial.json()["investment"]["status"] == "PARTIALLY_TERMINATED"
    assert partial.json()["investment"]["principal_amount"] == "200000000.00"

    over = await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "999999999", "termination_date": "2026-07-02"}, headers=headers,
    )
    assert over.status_code == 409

    full = await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "200000000", "termination_date": "2026-07-03"}, headers=headers,
    )
    assert full.status_code == 200
    assert full.json()["investment"]["status"] == "TERMINATED"
    assert full.json()["investment"]["principal_amount"] == "0.00"


async def test_rollover_creates_new_investment_preserves_original(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id)
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    rollover_resp = await client.post(
        f"/api/v1/investments/{inv_id}/rollover",
        json={"rollover_amount": "200000000", "new_rate": "19.0", "new_start_date": "2026-09-01",
              "new_maturity_date": "2026-12-01", "new_reference": "INV-API-001-R1"},
        headers=headers,
    )
    assert rollover_resp.status_code == 201
    new_investment = rollover_resp.json()
    assert new_investment["investment_reference"] == "INV-API-001-R1"
    assert new_investment["principal_amount"] == "200000000.00"
    assert new_investment["interest_rate"] == "19.000000"
    assert new_investment["status"] == "ACTIVE"

    original = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    original_body = original.json()
    assert original_body["interest_rate"] == "18.000000"
    assert original_body["maturity_date"] == "2026-09-01"
    assert original_body["status"] == "PARTIALLY_TERMINATED"
    assert original_body["principal_amount"] == "100000000.00"
    assert original_body["rolled_to_investment_id"] == new_investment["id"]

    new_investment_id = new_investment["id"]
    new_full = await client.get(f"/api/v1/investments/{new_investment_id}", headers=headers)
    assert new_full.json()["previous_investment_id"] == inv_id


async def test_rebooking_creates_new_version_not_silent_overwrite(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id)
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    rebook = await client.post(
        f"/api/v1/investments/{inv_id}/rebook",
        json={"new_rate": "20.0", "additional_principal": "50000000", "reason": "Top-up and repricing"},
        headers=headers,
    )
    assert rebook.status_code == 200
    assert rebook.json()["interest_rate"] == "20.000000"
    assert rebook.json()["principal_amount"] == "350000000.00"

    versions = await client.get(f"/api/v1/investments/{inv_id}/versions", headers=headers)
    version_list = versions.json()
    assert len(version_list) == 2
    assert version_list[0]["terms"]["interest_rate"] == "18.0"
    assert version_list[1]["terms"]["interest_rate"] == "20.0"
