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


async def _make_bank(db_session, name="Integrity Test Lender"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


def _facility_payload(entity_id, lender_id, **overrides):
    payload = {
        "facility_reference": "FAC-INTEGRITY-001", "facility_name": "Integrity Test Facility",
        "facility_type_code": "REVOLVING_CREDIT", "commitment_type": "COMMITTED",
        "lender_id": str(lender_id), "legal_entity_id": str(entity_id), "currency_code": "NGN",
        "approved_limit": "500000000", "committed_limit": "500000000",
        "interest_rate_type": "FIXED", "fixed_rate": "20.0",
        "start_date": "2026-01-01", "maturity_date": "2027-01-01", "repayment_method": "BULLET",
    }
    payload.update(overrides)
    return payload


async def _create_active_facility(client, headers, entity_id, bank_id, **overrides):
    resp = await client.post(
        "/api/v1/facilities", json=_facility_payload(entity_id, bank_id, **overrides), headers=headers,
    )
    facility_id = resp.json()["id"]
    await client.patch(f"/api/v1/facilities/{facility_id}/status", json={"new_status": "ACTIVE"}, headers=headers)
    return facility_id


async def _create_drawdown(client, headers, facility_id, entity_id, amount="50000000", currency="NGN"):
    resp = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_id), "currency_code": currency,
              "drawdown_amount": amount, "drawdown_date": "2026-02-01"},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_repayment(client, headers, facility_id, entity_id, amount="20000000", currency="NGN"):
    resp = await client.post(
        f"/api/v1/facilities/{facility_id}/repayments",
        json={"legal_entity_id": str(entity_id), "currency_code": currency,
              "repayment_type": "PRINCIPAL", "original_amount": amount, "due_date": "2026-06-01"},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_funding_action_cannot_link_both_drawdown_and_repayment(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    drawdown_id = await _create_drawdown(client, headers, facility_id, entity_a.id)
    repayment_id = await _create_repayment(client, headers, facility_id, entity_a.id)

    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id,
              "linked_repayment_id": repayment_id},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "both" in str(resp.json()["detail"]).lower()


async def test_action_type_must_match_the_kind_of_link(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    drawdown_id = await _create_drawdown(client, headers, facility_id, entity_a.id)

    wrong_type = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_REPAYMENT", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert wrong_type.status_code == 400

    unsupported_type = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_REFINANCING", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert unsupported_type.status_code == 400

    correct = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert correct.status_code == 201


async def test_linked_transaction_entity_mismatch_is_rejected(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    drawdown_id = await _create_drawdown(client, headers, facility_id, entity_a.id)

    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_b.id),
              "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "entity" in str(resp.json()["detail"]).lower()


async def test_linked_transaction_facility_mismatch_is_rejected(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_1_id = await _create_active_facility(
        client, headers, entity_a.id, bank.id, facility_reference="FAC-INTEGRITY-A",
    )
    facility_2_id = await _create_active_facility(
        client, headers, entity_a.id, bank.id, facility_reference="FAC-INTEGRITY-B",
    )
    drawdown_id = await _create_drawdown(client, headers, facility_1_id, entity_a.id)

    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_2_id, "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "facility" in str(resp.json()["detail"]).lower()


async def test_linked_transaction_amount_mismatch_is_rejected(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    drawdown_id = await _create_drawdown(client, headers, facility_id, entity_a.id, amount="50000000")

    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id, "amount": "99999999"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "amount" in str(resp.json()["detail"]).lower()

    ok = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert ok.status_code == 201


async def test_linked_transaction_currency_mismatch_is_rejected(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    drawdown_id = await _create_drawdown(client, headers, facility_id, entity_a.id, currency="NGN")

    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id, "currency_code": "USD"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "currency" in str(resp.json()["detail"]).lower()


async def test_only_one_funding_action_may_link_a_given_drawdown(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    drawdown_id = await _create_drawdown(client, headers, facility_id, entity_a.id)

    first = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert second.status_code == 400
    assert "already linked" in str(second.json()["detail"]).lower()


async def test_database_level_unique_constraint_backs_up_the_application_check(
    db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    import datetime

    from sqlalchemy.exc import IntegrityError

    from app.models.banking import Bank
    from app.models.facility import (
        CommitmentType,
        Facility,
        FacilityDrawdown,
        FacilityStatus,
        FundingAction,
        FundingActionType,
        InterestRateType,
        RepaymentMethod,
    )

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="DB Constraint Test Bank")
    db_session.add(bank)
    await db_session.flush()

    facility = Facility(
        facility_reference="FAC-DBCHECK-001", facility_name="DB Check Facility",
        facility_type_code="TERM_LOAN", commitment_type=CommitmentType.COMMITTED,
        lender_id=bank.id, legal_entity_id=entity_a.id, currency_code="NGN",
        approved_limit=Decimal(100000000), committed_limit=Decimal(100000000),
        interest_rate_type=InterestRateType.FIXED, fixed_rate=Decimal(20),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2027, 1, 1),
        repayment_method=RepaymentMethod.BULLET, status=FacilityStatus.ACTIVE,
    )
    db_session.add(facility)
    await db_session.flush()

    drawdown = FacilityDrawdown(
        facility_id=facility.id, legal_entity_id=entity_a.id, currency_code="NGN",
        drawdown_amount=Decimal(10000000), drawdown_date=datetime.date(2026, 2, 1),
    )
    db_session.add(drawdown)
    await db_session.flush()

    db_session.add(FundingAction(
        action_type=FundingActionType.PROPOSE_DRAWDOWN, legal_entity_id=entity_a.id,
        facility_id=facility.id, linked_drawdown_id=drawdown.id,
    ))
    await db_session.flush()

    db_session.add(FundingAction(
        action_type=FundingActionType.PROPOSE_DRAWDOWN, legal_entity_id=entity_a.id,
        facility_id=facility.id, linked_drawdown_id=drawdown.id,
    ))
    try:
        await db_session.flush()
        raised = False
    except IntegrityError:
        raised = True
    assert raised
    await db_session.rollback()


async def test_repayment_linked_action_requires_full_payment_not_partial(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    repayment_id = await _create_repayment(client, headers, facility_id, entity_a.id, amount="20000000")

    action_resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_REPAYMENT", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_repayment_id": repayment_id},
        headers=headers,
    )
    assert action_resp.status_code == 201
    action_id = action_resp.json()["id"]

    await client.post(f"/api/v1/funding/actions/{action_id}/submit", headers=headers)
    await client.post(f"/api/v1/funding/actions/{action_id}/review", headers=headers)
    await client.post(f"/api/v1/funding/actions/{action_id}/approve", headers=headers)

    await client.post(
        f"/api/v1/repayments/{repayment_id}/pay",
        json={"paid_amount": "10000000", "actual_payment_date": "2026-06-01"}, headers=headers,
    )
    partial_execute = await client.post(f"/api/v1/funding/actions/{action_id}/execute", headers=headers)
    assert partial_execute.status_code == 409

    await client.post(
        f"/api/v1/repayments/{repayment_id}/pay",
        json={"paid_amount": "10000000", "actual_payment_date": "2026-06-02"}, headers=headers,
    )
    full_execute = await client.post(f"/api/v1/funding/actions/{action_id}/execute", headers=headers)
    assert full_execute.status_code == 200
    assert full_execute.json()["status"] == "EXECUTED"


async def test_drawdown_linked_action_full_lifecycle_with_valid_linkage(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    drawdown_id = await _create_drawdown(client, headers, facility_id, entity_a.id)

    action_resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "facility_id": facility_id, "linked_drawdown_id": drawdown_id},
        headers=headers,
    )
    assert action_resp.status_code == 201
    action_id = action_resp.json()["id"]
    assert action_resp.json()["linked_drawdown_id"] == drawdown_id

    await client.post(f"/api/v1/funding/actions/{action_id}/submit", headers=headers)
    await client.post(f"/api/v1/funding/actions/{action_id}/review", headers=headers)
    await client.post(f"/api/v1/funding/actions/{action_id}/approve", headers=headers)

    too_early = await client.post(f"/api/v1/funding/actions/{action_id}/execute", headers=headers)
    assert too_early.status_code == 409

    await client.post(f"/api/v1/drawdowns/{drawdown_id}/approve", headers=headers)
    await client.post(f"/api/v1/drawdowns/{drawdown_id}/execute", headers=headers)

    now_ok = await client.post(f"/api/v1/funding/actions/{action_id}/execute", headers=headers)
    assert now_ok.status_code == 200
    assert now_ok.json()["status"] == "EXECUTED"


# ---------------------------------------------------------------------------
# SECTION 9: cross-entity / cross-group security for linked transactions
# ---------------------------------------------------------------------------

async def _make_scoped_user(db_session, *, scope_type, legal_entity_id=None, group_id=None, label):
    import uuid as uuid_module

    from app.core.security import hash_password
    from app.models.rbac import Permission, Role, TreasuryAction, TreasuryModule, UserRoleAssignment

    role = Role(name=f"{label}-role-{uuid_module.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for action in (
        TreasuryAction.VIEW, TreasuryAction.CREATE, TreasuryAction.EDIT, TreasuryAction.APPROVE,
        TreasuryAction.EXECUTE, TreasuryAction.SUBMIT, TreasuryAction.INVESTIGATE, TreasuryAction.CLOSE,
    ):
        db_session.add(Permission(role_id=role.id, module=TreasuryModule.FACILITIES, action=action))
    user = User(
        email=f"{label}-{uuid_module.uuid4().hex[:8]}@treasuryos.example.com",
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


async def test_entity_a_cannot_create_funding_action_for_entity_b_facility(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fa-usera",
    )
    await db_session.commit()
    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_b.id)},
        headers=headers_a,
    )
    assert resp.status_code == 403


async def test_entity_a_cannot_link_funding_action_to_entity_b_drawdown_via_direct_id(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    """
    Direct-ID manipulation: even if User A somehow learns Entity B's
    drawdown id (e.g. a leaked/guessed UUID), creating a FundingAction
    for Entity A that links to it must be rejected by server-side
    validation - frontend filtering is not security.
    """
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fa-usera2",
    )
    user_b = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_b.id, label="fa-userb2",
    )
    await db_session.commit()

    headers_b = {"Authorization": f"Bearer {await _login(client, user_b)}"}
    facility_b_id = await _create_active_facility(client, headers_b, entity_b.id, bank.id)
    drawdown_b_id = await _create_drawdown(client, headers_b, facility_b_id, entity_b.id)

    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}
    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "linked_drawdown_id": drawdown_b_id},
        headers=headers_a,
    )
    # Rejected either as a 403 (no view/create rights on Entity B's data)
    # or a 400 (entity mismatch caught by validate_funding_action) -
    # both are correct outcomes; what matters is it is never 201.
    assert resp.status_code in (400, 403)


async def test_entity_a_cannot_link_funding_action_to_entity_b_repayment_via_direct_id(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fa-usera3",
    )
    user_b = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_b.id, label="fa-userb3",
    )
    await db_session.commit()

    headers_b = {"Authorization": f"Bearer {await _login(client, user_b)}"}
    facility_b_id = await _create_active_facility(client, headers_b, entity_b.id, bank.id)
    repayment_b_id = await _create_repayment(client, headers_b, facility_b_id, entity_b.id)

    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}
    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_REPAYMENT", "legal_entity_id": str(entity_a.id),
              "linked_repayment_id": repayment_b_id},
        headers=headers_a,
    )
    assert resp.status_code in (400, 403)


async def test_cross_group_funding_action_linkage_is_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    """Group 1 cannot create a FundingAction linked to Group 2's facility/transaction."""
    from app.models.entity import Group, LegalEntity
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)

    group2 = Group(name="FA Second Group", code="FAGRP2", reporting_currency_code="USD")
    db_session.add(group2)
    await db_session.flush()
    entity_c = LegalEntity(
        group_id=group2.id, name="FA Entity C", code="FAENTC", functional_currency_code="USD", country="US",
    )
    db_session.add(entity_c)
    await db_session.flush()

    user_group2 = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_c.id, label="fa-group2user",
    )
    user_group1 = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fa-group1user",
    )
    await db_session.commit()

    headers_group2 = {"Authorization": f"Bearer {await _login(client, user_group2)}"}
    facility_c_id = await _create_active_facility(client, headers_group2, entity_c.id, bank.id)
    drawdown_c_id = await _create_drawdown(client, headers_group2, facility_c_id, entity_c.id)

    headers_group1 = {"Authorization": f"Bearer {await _login(client, user_group1)}"}
    resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "linked_drawdown_id": drawdown_c_id},
        headers=headers_group1,
    )
    assert resp.status_code in (400, 403)
