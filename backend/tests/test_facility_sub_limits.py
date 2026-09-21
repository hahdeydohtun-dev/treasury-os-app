from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _make_bank(db_session, name="Sub-Limit Test Lender"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


def _facility_payload(entity_id, lender_id, **overrides):
    payload = {
        "facility_reference": "FAC-SUBLIMIT-001", "facility_name": "Sub-Limit Test Facility",
        "facility_type_code": "REVOLVING_CREDIT", "commitment_type": "COMMITTED",
        "lender_id": str(lender_id), "legal_entity_id": str(entity_id), "currency_code": "NGN",
        "approved_limit": "500000000", "committed_limit": "500000000",
        "interest_rate_type": "FIXED", "fixed_rate": "20.0",
        "start_date": "2026-01-01", "maturity_date": "2027-01-01", "repayment_method": "BULLET",
    }
    payload.update(overrides)
    return payload


async def test_sub_limit_amount_cannot_exceed_facility_committed_limit(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/facilities", json=_facility_payload(entity_a.id, bank.id), headers=headers,
    )
    facility_id = create_resp.json()["id"]

    over_limit_sub = await client.post(
        f"/api/v1/facilities/{facility_id}/sub-limits",
        json={"name": "Too Big", "limit_amount": "600000000"}, headers=headers,
    )
    assert over_limit_sub.status_code == 400

    valid_sub = await client.post(
        f"/api/v1/facilities/{facility_id}/sub-limits",
        json={"name": "Working Capital Tranche", "purpose_code": "WORKING_CAPITAL",
              "limit_amount": "100000000"},
        headers=headers,
    )
    assert valid_sub.status_code == 201
    assert valid_sub.json()["drawn_amount"] == "0.00"


async def test_drawdown_within_facility_limit_but_exceeding_sub_limit_is_rejected(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/facilities", json=_facility_payload(entity_a.id, bank.id), headers=headers,
    )
    facility_id = create_resp.json()["id"]
    await client.patch(f"/api/v1/facilities/{facility_id}/status", json={"new_status": "ACTIVE"}, headers=headers)

    sub_limit_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/sub-limits",
        json={"name": "Working Capital Tranche", "limit_amount": "50000000"}, headers=headers,
    )
    sub_limit_id = sub_limit_resp.json()["id"]

    over_sub_limit = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "sub_limit_id": sub_limit_id,
              "currency_code": "NGN", "drawdown_amount": "80000000", "drawdown_date": "2026-02-01"},
        headers=headers,
    )
    assert over_sub_limit.status_code == 400
    assert "sub-limit" in str(over_sub_limit.json()["detail"]).lower()

    within_sub_limit = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "sub_limit_id": sub_limit_id,
              "currency_code": "NGN", "drawdown_amount": "30000000", "drawdown_date": "2026-02-01"},
        headers=headers,
    )
    assert within_sub_limit.status_code == 201


async def test_executed_drawdown_increases_sub_limit_drawn_and_repayment_frees_it_up(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/facilities", json=_facility_payload(entity_a.id, bank.id), headers=headers,
    )
    facility_id = create_resp.json()["id"]
    await client.patch(f"/api/v1/facilities/{facility_id}/status", json={"new_status": "ACTIVE"}, headers=headers)

    sub_limit_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/sub-limits",
        json={"name": "Working Capital Tranche", "limit_amount": "50000000"}, headers=headers,
    )
    sub_limit_id = sub_limit_resp.json()["id"]

    drawdown_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "sub_limit_id": sub_limit_id,
              "currency_code": "NGN", "drawdown_amount": "40000000", "drawdown_date": "2026-02-01"},
        headers=headers,
    )
    drawdown_id = drawdown_resp.json()["id"]
    await client.post(f"/api/v1/drawdowns/{drawdown_id}/approve", headers=headers)
    await client.post(f"/api/v1/drawdowns/{drawdown_id}/execute", headers=headers)

    sub_limits_after_draw = await client.get(
        f"/api/v1/facilities/{facility_id}/sub-limits", headers=headers
    )
    assert sub_limits_after_draw.json()[0]["drawn_amount"] == "40000000.00"

    second_drawdown = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "sub_limit_id": sub_limit_id,
              "currency_code": "NGN", "drawdown_amount": "20000000", "drawdown_date": "2026-02-02"},
        headers=headers,
    )
    assert second_drawdown.status_code == 400

    repayment_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/repayments",
        json={"legal_entity_id": str(entity_a.id), "drawdown_id": drawdown_id,
              "currency_code": "NGN", "repayment_type": "PRINCIPAL",
              "original_amount": "40000000", "due_date": "2026-06-01"},
        headers=headers,
    )
    repayment_id = repayment_resp.json()["id"]
    await client.post(
        f"/api/v1/repayments/{repayment_id}/pay",
        json={"paid_amount": "40000000", "actual_payment_date": "2026-06-01"}, headers=headers,
    )

    sub_limits_after_repay = await client.get(
        f"/api/v1/facilities/{facility_id}/sub-limits", headers=headers
    )
    assert sub_limits_after_repay.json()[0]["drawn_amount"] == "0.00"

    third_drawdown = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "sub_limit_id": sub_limit_id,
              "currency_code": "NGN", "drawdown_amount": "45000000", "drawdown_date": "2026-06-02"},
        headers=headers,
    )
    assert third_drawdown.status_code == 201
