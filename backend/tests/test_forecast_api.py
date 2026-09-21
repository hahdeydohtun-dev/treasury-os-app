from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_create_calculate_publish_lifecycle(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/forecast",
        json={
            "group_id": str(group.id), "forecast_start_date": "2026-06-01",
            "scenario": "BASE", "value_basis": "GROSS", "reporting_currency_code": "NGN",
        },
        headers=headers,
    )
    assert create_resp.status_code == 201
    forecast_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "DRAFT"

    calc_resp = await client.post(f"/api/v1/forecast/{forecast_id}/calculate", headers=headers)
    assert calc_resp.status_code == 200

    weeks_resp = await client.get(f"/api/v1/forecast/{forecast_id}/weeks", headers=headers)
    assert weeks_resp.status_code == 200
    assert len(weeks_resp.json()) == 13

    summary_resp = await client.get(f"/api/v1/forecast/{forecast_id}/summary", headers=headers)
    assert summary_resp.status_code == 200
    assert "lowest_projected_cash" in summary_resp.json()

    publish_resp = await client.post(f"/api/v1/forecast/{forecast_id}/publish", headers=headers)
    assert publish_resp.status_code == 200
    assert publish_resp.json()["status"] == "PUBLISHED"

    recalc_after_publish = await client.post(
        f"/api/v1/forecast/{forecast_id}/calculate", headers=headers
    )
    assert recalc_after_publish.status_code == 400


async def test_roll_forward_creates_next_version_linked_to_parent(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/forecast",
        json={
            "group_id": str(group.id), "forecast_start_date": "2026-06-01",
            "reporting_currency_code": "NGN",
        },
        headers=headers,
    )
    forecast_id = create_resp.json()["id"]

    roll_resp = await client.post(f"/api/v1/forecast/{forecast_id}/roll", headers=headers)
    assert roll_resp.status_code == 201
    rolled = roll_resp.json()
    assert rolled["forecast_start_date"] == "2026-06-08"
    assert rolled["version"] == 2
    assert rolled["parent_forecast_id"] == forecast_id


async def test_entity_scoped_user_cannot_create_other_entity_forecast(
    client: AsyncClient, db_session: AsyncSession, entity_a_scoped_user: User,
    demo_group_and_entities, forecast_categories,
):
    from app.models.rbac import Permission, TreasuryAction, TreasuryModule, UserRoleAssignment

    group, entity_a, entity_b = demo_group_and_entities

    # Grant this user's existing role FORECAST_13WK permissions too, so we're
    # testing entity scoping specifically, not an unrelated missing-module grant.
    assignment_result = await db_session.execute(
        select(UserRoleAssignment).where(UserRoleAssignment.user_id == entity_a_scoped_user.id)
    )
    role_id = assignment_result.scalars().first().role_id
    for action in (
        TreasuryAction.VIEW, TreasuryAction.CREATE, TreasuryAction.EDIT, TreasuryAction.ADJUST,
    ):
        db_session.add(Permission(role_id=role_id, module=TreasuryModule.FORECAST_13WK, action=action))
    await db_session.commit()

    token = await _login(client, entity_a_scoped_user)
    headers = {"Authorization": f"Bearer {token}"}

    create_a = await client.post(
        "/api/v1/forecast",
        json={
            "legal_entity_id": str(entity_a.id), "forecast_start_date": "2026-06-01",
            "reporting_currency_code": "NGN",
        },
        headers=headers,
    )
    assert create_a.status_code == 201

    create_b = await client.post(
        "/api/v1/forecast",
        json={
            "legal_entity_id": str(entity_b.id), "forecast_start_date": "2026-06-01",
            "reporting_currency_code": "USD",
        },
        headers=headers,
    )
    assert create_b.status_code == 403


async def test_forecast_requires_authentication(client: AsyncClient):
    resp = await client.get("/api/v1/forecast")
    assert resp.status_code == 401


async def test_variance_and_accuracy_endpoints_respond(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, forecast_categories,
):
    group, entity_a, _ = demo_group_and_entities
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/forecast",
        json={
            "legal_entity_id": str(entity_a.id), "forecast_start_date": "2026-06-01",
            "reporting_currency_code": "NGN",
        },
        headers=headers,
    )
    forecast_id = create_resp.json()["id"]
    await client.post(f"/api/v1/forecast/{forecast_id}/calculate", headers=headers)

    variance_resp = await client.get(
        f"/api/v1/forecast/{forecast_id}/variance", params={"group_by": "category"},
        headers=headers,
    )
    assert variance_resp.status_code == 200

    accuracy_resp = await client.get(f"/api/v1/forecast/{forecast_id}/accuracy", headers=headers)
    assert accuracy_resp.status_code == 200
    assert accuracy_resp.json()["target_variance_pct"] == "5"


async def test_liquidity_gap_generates_alert(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, forecast_categories,
):
    from app.models.forecast import LiquidityScopeType, LiquidityThreshold

    group, entity_a, _ = demo_group_and_entities
    db_session.add(LiquidityThreshold(
        scope_type=LiquidityScopeType.ENTITY, legal_entity_id=entity_a.id,
        minimum_amount=Decimal(999999999),
    ))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/forecast",
        json={
            "legal_entity_id": str(entity_a.id), "forecast_start_date": "2026-06-01",
            "reporting_currency_code": "NGN",
        },
        headers=headers,
    )
    forecast_id = create_resp.json()["id"]
    await client.post(f"/api/v1/forecast/{forecast_id}/calculate", headers=headers)

    alerts_resp = await client.get(f"/api/v1/forecast/{forecast_id}/alerts", headers=headers)
    assert alerts_resp.status_code == 200
    alerts = alerts_resp.json()
    assert len(alerts) > 0
    assert any(a["metric"] == "LIQUIDITY_GAP" for a in alerts)

    gaps_resp = await client.get(f"/api/v1/forecast/{forecast_id}/funding-gaps", headers=headers)
    assert len(gaps_resp.json()) == 13
