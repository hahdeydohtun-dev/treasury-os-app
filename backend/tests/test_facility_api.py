
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _make_bank(db_session, name="Test Lender"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


def _facility_payload(entity_id, lender_id, **overrides):
    payload = {
        "facility_reference": "FAC-API-001", "facility_name": "API Test Facility",
        "facility_type_code": "TERM_LOAN", "commitment_type": "COMMITTED",
        "lender_id": str(lender_id), "legal_entity_id": str(entity_id), "currency_code": "NGN",
        "approved_limit": "500000000", "committed_limit": "500000000",
        "interest_rate_type": "FIXED", "fixed_rate": "20.0",
        "start_date": "2026-01-01", "maturity_date": "2027-01-01",
        "repayment_method": "BULLET",
    }
    payload.update(overrides)
    return payload


async def test_create_facility_and_lifecycle(
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
    assert create_resp.status_code == 201
    facility = create_resp.json()
    assert facility["status"] == "DRAFT"
    facility_id = facility["id"]

    bad_transition = await client.patch(
        f"/api/v1/facilities/{facility_id}/status", json={"new_status": "CLOSED"}, headers=headers,
    )
    assert bad_transition.status_code == 400

    activate_resp = await client.patch(
        f"/api/v1/facilities/{facility_id}/status", json={"new_status": "ACTIVE"}, headers=headers,
    )
    assert activate_resp.status_code == 200
    assert activate_resp.json()["status"] == "ACTIVE"

    events_resp = await client.get(f"/api/v1/facilities/{facility_id}/events", headers=headers)
    event_types = {e["event_type"] for e in events_resp.json()}
    assert "FACILITY_CREATED" in event_types
    assert "FACILITY_ACTIVATED" in event_types


async def test_facility_update_creates_new_version_never_overwrites(
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

    update_resp = await client.patch(
        f"/api/v1/facilities/{facility_id}",
        json={"committed_limit": "750000000", "fixed_rate": "18.5", "change_reason": "Annual repricing"},
        headers=headers,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["committed_limit"] == "750000000.00"
    assert update_resp.json()["version"] == 2

    versions_resp = await client.get(f"/api/v1/facilities/{facility_id}/versions", headers=headers)
    versions = versions_resp.json()
    assert len(versions) == 2
    assert versions[0]["terms"]["committed_limit"] == "500000000"
    assert versions[1]["terms"]["committed_limit"] == "750000000"


async def test_drawdown_exceeding_limit_is_rejected(
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

    over_limit = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "currency_code": "NGN",
              "drawdown_amount": "600000000", "drawdown_date": "2026-02-01"},
        headers=headers,
    )
    assert over_limit.status_code == 400

    valid = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "currency_code": "NGN",
              "drawdown_amount": "200000000", "drawdown_date": "2026-02-01"},
        headers=headers,
    )
    assert valid.status_code == 201
    drawdown_id = valid.json()["id"]

    inactive_facility_resp = await client.post(
        "/api/v1/facilities", json=_facility_payload(entity_a.id, bank.id, facility_reference="FAC-DRAFT"),
        headers=headers,
    )
    draft_facility_id = inactive_facility_resp.json()["id"]
    draft_drawdown = await client.post(
        f"/api/v1/facilities/{draft_facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "currency_code": "NGN",
              "drawdown_amount": "1000000", "drawdown_date": "2026-02-01"},
        headers=headers,
    )
    assert draft_drawdown.status_code == 400

    approve_resp = await client.post(f"/api/v1/drawdowns/{drawdown_id}/approve", headers=headers)
    assert approve_resp.status_code == 200
    execute_resp = await client.post(f"/api/v1/drawdowns/{drawdown_id}/execute", headers=headers)
    assert execute_resp.status_code == 200

    utilization = await client.get(f"/api/v1/facilities/{facility_id}/utilization", headers=headers)
    assert utilization.json()["drawn_amount"] == "200000000.00"


async def test_repayment_partial_then_full(
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

    repayment_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/repayments",
        json={"legal_entity_id": str(entity_a.id), "currency_code": "NGN",
              "repayment_type": "PRINCIPAL", "original_amount": "100000000", "due_date": "2026-06-01"},
        headers=headers,
    )
    assert repayment_resp.status_code == 201
    repayment_id = repayment_resp.json()["id"]

    partial = await client.post(
        f"/api/v1/repayments/{repayment_id}/pay",
        json={"paid_amount": "40000000", "actual_payment_date": "2026-06-01"}, headers=headers,
    )
    assert partial.status_code == 200
    assert partial.json()["status"] == "PARTIALLY_PAID"

    full = await client.post(
        f"/api/v1/repayments/{repayment_id}/pay",
        json={"paid_amount": "60000000", "actual_payment_date": "2026-06-02"}, headers=headers,
    )
    assert full.status_code == 200
    assert full.json()["status"] == "PAID"
    assert full.json()["paid_amount"] == "100000000.00"


async def test_covenant_data_required_until_measured(
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

    covenant_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/covenants",
        json={"name": "Min Liquidity", "covenant_type": "MINIMUM_LIQUIDITY", "operator": "GTE",
              "threshold": "200000000", "warning_threshold": "250000000"},
        headers=headers,
    )
    assert covenant_resp.status_code == 201
    assert covenant_resp.json()["status"] == "DATA_REQUIRED"
    covenant_id = covenant_resp.json()["id"]

    breach_update = await client.patch(
        f"/api/v1/covenants/{covenant_id}",
        json={"current_value": "150000000", "measurement_date": "2026-03-01"}, headers=headers,
    )
    assert breach_update.status_code == 200
    assert breach_update.json()["status"] == "BREACH"

    warning_update = await client.patch(
        f"/api/v1/covenants/{covenant_id}",
        json={"current_value": "220000000", "measurement_date": "2026-04-01"}, headers=headers,
    )
    assert warning_update.status_code == 200
    assert warning_update.json()["status"] == "WARNING"

    compliant_update = await client.patch(
        f"/api/v1/covenants/{covenant_id}",
        json={"current_value": "300000000", "measurement_date": "2026-05-01"}, headers=headers,
    )
    assert compliant_update.status_code == 200
    assert compliant_update.json()["status"] == "COMPLIANT"


async def test_collateral_eligible_value_applies_haircut(
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

    collateral_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/collateral",
        json={"collateral_type": "RECEIVABLES", "value": "100000000", "currency_code": "NGN",
              "valuation_date": "2026-01-01", "haircut_pct": "20"},
        headers=headers,
    )
    assert collateral_resp.status_code == 201
    assert collateral_resp.json()["eligible_value"] == "80000000.00"
