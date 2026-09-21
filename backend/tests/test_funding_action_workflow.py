from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _create_action(client: AsyncClient, headers: dict, entity_id) -> str:
    resp = await client.post(
        "/api/v1/funding/actions",
        json={
            "action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_id),
            "amount": "50000000", "currency_code": "NGN",
            "rationale": "Cover the projected week 5 liquidity gap.",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "DRAFT"
    return resp.json()["id"]


async def test_funding_action_full_lifecycle_submit_review_approve_execute_complete(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User, demo_group_and_entities,
):
    group, entity_a, _ = demo_group_and_entities
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    action_id = await _create_action(client, headers, entity_a.id)

    submit = await client.post(f"/api/v1/funding/actions/{action_id}/submit", headers=headers)
    assert submit.status_code == 200
    assert submit.json()["status"] == "SUBMITTED"

    review = await client.post(f"/api/v1/funding/actions/{action_id}/review", headers=headers)
    assert review.status_code == 200
    assert review.json()["status"] == "UNDER_REVIEW"

    approve = await client.post(f"/api/v1/funding/actions/{action_id}/approve", headers=headers)
    assert approve.status_code == 200
    assert approve.json()["status"] == "APPROVED"

    execute = await client.post(f"/api/v1/funding/actions/{action_id}/execute", headers=headers)
    assert execute.status_code == 200
    assert execute.json()["status"] == "EXECUTED"

    complete = await client.post(f"/api/v1/funding/actions/{action_id}/complete", headers=headers)
    assert complete.status_code == 200
    assert complete.json()["status"] == "COMPLETED"

    re_cancel = await client.post(f"/api/v1/funding/actions/{action_id}/cancel", headers=headers)
    assert re_cancel.status_code == 400


async def test_funding_action_illegal_transitions_are_rejected(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User, demo_group_and_entities,
):
    group, entity_a, _ = demo_group_and_entities
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    action_id = await _create_action(client, headers, entity_a.id)

    bad = await client.post(f"/api/v1/funding/actions/{action_id}/execute", headers=headers)
    assert bad.status_code == 400
    assert "DRAFT" in bad.json()["detail"]

    bad2 = await client.post(f"/api/v1/funding/actions/{action_id}/approve", headers=headers)
    assert bad2.status_code == 400


async def test_funding_action_can_be_rejected_and_rejection_is_terminal(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User, demo_group_and_entities,
):
    group, entity_a, _ = demo_group_and_entities
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    action_id = await _create_action(client, headers, entity_a.id)
    await client.post(f"/api/v1/funding/actions/{action_id}/submit", headers=headers)

    reject = await client.post(f"/api/v1/funding/actions/{action_id}/reject", headers=headers)
    assert reject.status_code == 200
    assert reject.json()["status"] == "REJECTED"

    approve_after_reject = await client.post(
        f"/api/v1/funding/actions/{action_id}/approve", headers=headers
    )
    assert approve_after_reject.status_code == 400


async def test_funding_action_cancel_allowed_before_execution_only(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User, demo_group_and_entities,
):
    group, entity_a, _ = demo_group_and_entities
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    action_id = await _create_action(client, headers, entity_a.id)
    cancel = await client.post(f"/api/v1/funding/actions/{action_id}/cancel", headers=headers)
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "CANCELLED"


async def test_entity_a_cannot_transition_entity_b_funding_action(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities,
):
    import uuid as uuid_module

    from app.core.security import hash_password
    from app.models.rbac import (
        EntityScopeType,
        Permission,
        Role,
        TreasuryAction,
        TreasuryModule,
        UserRoleAssignment,
    )

    group, entity_a, entity_b = demo_group_and_entities

    role = Role(name=f"usera-fa-role-{uuid_module.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for action in (
        TreasuryAction.VIEW, TreasuryAction.CREATE, TreasuryAction.SUBMIT, TreasuryAction.INVESTIGATE,
        TreasuryAction.APPROVE, TreasuryAction.EXECUTE, TreasuryAction.CLOSE, TreasuryAction.EDIT,
    ):
        db_session.add(Permission(role_id=role.id, module=TreasuryModule.FACILITIES, action=action))
    user_a = User(
        email=f"usera-fa-{uuid_module.uuid4().hex[:8]}@treasuryos.example.com",
        full_name="User A", hashed_password=hash_password("Password123!"), is_superuser=False,
    )
    db_session.add(user_a)
    await db_session.flush()
    db_session.add(UserRoleAssignment(
        user_id=user_a.id, role_id=role.id, scope_type=EntityScopeType.ENTITY,
        legal_entity_id=entity_a.id,
    ))
    await db_session.commit()

    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    from app.models.facility import FundingAction, FundingActionStatus, FundingActionType

    action_b = FundingAction(
        action_type=FundingActionType.PROPOSE_DRAWDOWN, legal_entity_id=entity_b.id,
        status=FundingActionStatus.DRAFT,
    )
    db_session.add(action_b)
    await db_session.commit()

    detail = await client.get(f"/api/v1/funding/actions/{action_b.id}", headers=headers_a)
    assert detail.status_code == 403

    submit_attempt = await client.post(
        f"/api/v1/funding/actions/{action_b.id}/submit", headers=headers_a
    )
    assert submit_attempt.status_code == 403
