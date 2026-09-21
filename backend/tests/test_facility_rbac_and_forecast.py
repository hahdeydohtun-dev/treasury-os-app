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


async def _make_scoped_user(db_session, *, scope_type, legal_entity_id=None, group_id=None, label):
    role = Role(name=f"{label}-role-{uuid.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for action in (
        TreasuryAction.VIEW, TreasuryAction.CREATE, TreasuryAction.EDIT, TreasuryAction.APPROVE,
        TreasuryAction.EXECUTE, TreasuryAction.CONFIGURE, TreasuryAction.EXPORT,
    ):
        db_session.add(Permission(role_id=role.id, module=TreasuryModule.FACILITIES, action=action))

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
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _make_bank(db_session, name="RBAC Test Bank"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


def _facility_payload(entity_id, lender_id, **overrides):
    payload = {
        "facility_reference": f"FAC-RBAC-{uuid.uuid4().hex[:6]}", "facility_name": "RBAC Test Facility",
        "facility_type_code": "TERM_LOAN", "commitment_type": "COMMITTED",
        "lender_id": str(lender_id), "legal_entity_id": str(entity_id), "currency_code": "NGN",
        "approved_limit": "500000000", "committed_limit": "500000000",
        "interest_rate_type": "FIXED", "fixed_rate": "20.0",
        "start_date": "2026-01-01", "maturity_date": "2027-01-01", "repayment_method": "BULLET",
    }
    payload.update(overrides)
    return payload


async def test_entity_a_cannot_see_or_modify_entity_b_facility(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    await db_session.commit()
    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    create_b = await client.post(
        "/api/v1/facilities", json=_facility_payload(entity_b.id, bank.id), headers=headers_a,
    )
    assert create_b.status_code == 403

    from app.models.facility import (
        CommitmentType,
        Facility,
        FacilityStatus,
        InterestRateType,
        RepaymentMethod,
    )

    facility_b = Facility(
        facility_reference="FAC-B-001", facility_name="Entity B Facility",
        facility_type_code="TERM_LOAN", commitment_type=CommitmentType.COMMITTED,
        lender_id=bank.id, legal_entity_id=entity_b.id, currency_code="USD",
        approved_limit=Decimal(1000000), committed_limit=Decimal(1000000),
        interest_rate_type=InterestRateType.FIXED, fixed_rate=Decimal(10),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2027, 1, 1),
        repayment_method=RepaymentMethod.BULLET, status=FacilityStatus.ACTIVE,
    )
    db_session.add(facility_b)
    await db_session.commit()

    list_resp = await client.get("/api/v1/facilities", headers=headers_a)
    assert str(facility_b.id) not in {f["id"] for f in list_resp.json()}

    detail_resp = await client.get(f"/api/v1/facilities/{facility_b.id}", headers=headers_a)
    assert detail_resp.status_code == 403

    update_resp = await client.patch(
        f"/api/v1/facilities/{facility_b.id}",
        json={"committed_limit": "2000000", "change_reason": "attempted cross-entity edit"},
        headers=headers_a,
    )
    assert update_resp.status_code == 403

    drawdown_resp = await client.post(
        f"/api/v1/facilities/{facility_b.id}/drawdowns",
        json={"legal_entity_id": str(entity_b.id), "currency_code": "USD",
              "drawdown_amount": "1000", "drawdown_date": "2026-02-01"},
        headers=headers_a,
    )
    assert drawdown_resp.status_code == 403


async def test_entity_a_cannot_see_entity_b_drawdown_or_repayment(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera",
    )
    user_b = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_b.id, label="userb",
    )
    await db_session.commit()

    headers_b = {"Authorization": f"Bearer {await _login(client, user_b)}"}
    create_resp = await client.post(
        "/api/v1/facilities", json=_facility_payload(entity_b.id, bank.id), headers=headers_b,
    )
    facility_id = create_resp.json()["id"]
    await client.patch(f"/api/v1/facilities/{facility_id}/status", json={"new_status": "ACTIVE"}, headers=headers_b)
    drawdown_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_b.id), "currency_code": "NGN",
              "drawdown_amount": "1000000", "drawdown_date": "2026-02-01"},
        headers=headers_b,
    )
    drawdown_id = drawdown_resp.json()["id"]
    repayment_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/repayments",
        json={"legal_entity_id": str(entity_b.id), "currency_code": "NGN",
              "repayment_type": "PRINCIPAL", "original_amount": "500000", "due_date": "2026-06-01"},
        headers=headers_b,
    )
    repayment_id = repayment_resp.json()["id"]

    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}
    assert (await client.get(f"/api/v1/drawdowns/{drawdown_id}", headers=headers_a)).status_code == 403
    assert (await client.get(f"/api/v1/repayments/{repayment_id}", headers=headers_a)).status_code == 403
    assert (await client.get(
        f"/api/v1/facilities/{facility_id}/drawdowns", headers=headers_a
    )).status_code == 403


async def test_group_user_sees_authorized_entities_cross_group_blocked(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    from app.models.entity import Group, LegalEntity
    from app.models.facility import (
        CommitmentType,
        Facility,
        FacilityStatus,
        InterestRateType,
        RepaymentMethod,
    )

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)

    group2 = Group(name="Second Group", code="RBACGRP2", reporting_currency_code="USD")
    db_session.add(group2)
    await db_session.flush()
    entity_c = LegalEntity(
        group_id=group2.id, name="Entity C", code="RBACENTC", functional_currency_code="USD", country="US",
    )
    db_session.add(entity_c)
    await db_session.flush()

    facility_a = Facility(
        facility_reference="FAC-GROUP-A", facility_name="Entity A Facility",
        facility_type_code="TERM_LOAN", commitment_type=CommitmentType.COMMITTED,
        lender_id=bank.id, legal_entity_id=entity_a.id, currency_code="NGN",
        approved_limit=Decimal(1000000), committed_limit=Decimal(1000000),
        interest_rate_type=InterestRateType.FIXED, fixed_rate=Decimal(10),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2027, 1, 1),
        repayment_method=RepaymentMethod.BULLET, status=FacilityStatus.ACTIVE,
    )
    facility_c = Facility(
        facility_reference="FAC-GROUP-C", facility_name="Entity C Facility",
        facility_type_code="TERM_LOAN", commitment_type=CommitmentType.COMMITTED,
        lender_id=bank.id, legal_entity_id=entity_c.id, currency_code="USD",
        approved_limit=Decimal(2000000), committed_limit=Decimal(2000000),
        interest_rate_type=InterestRateType.FIXED, fixed_rate=Decimal(8),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2027, 1, 1),
        repayment_method=RepaymentMethod.BULLET, status=FacilityStatus.ACTIVE,
    )
    db_session.add_all([facility_a, facility_c])

    group_user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group.id, label="groupuser",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, group_user)}"}

    list_resp = await client.get("/api/v1/facilities", headers=headers)
    ids = {f["id"] for f in list_resp.json()}
    assert str(facility_a.id) in ids
    assert str(facility_c.id) not in ids

    assert (await client.get(f"/api/v1/facilities/{facility_c.id}", headers=headers)).status_code == 403
    assert (await client.get(f"/api/v1/facilities/{facility_a.id}", headers=headers)).status_code == 200


async def test_facility_events_feed_the_forecast_engine(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, facility_types,
):
    from app.models.banking import Bank
    from app.models.facility import (
        CommitmentType,
        DrawdownStatus,
        Facility,
        FacilityDrawdown,
        FacilityFee,
        FacilityRepayment,
        FacilityStatus,
        FeeStatus,
        FeeType,
        InterestRateType,
        RepaymentMethod,
        RepaymentType,
    )
    from app.models.forecast import Forecast, ForecastStatus, ForecastValueBasis
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="Forecast Integration Bank")
    db_session.add(bank)
    await db_session.flush()

    facility = Facility(
        facility_reference="FAC-FCST-001", facility_name="Forecast Test Facility",
        facility_type_code="TERM_LOAN", commitment_type=CommitmentType.COMMITTED,
        lender_id=bank.id, legal_entity_id=entity_a.id, currency_code="NGN",
        approved_limit=Decimal(500000000), committed_limit=Decimal(500000000),
        current_drawn_amount=Decimal(200000000),
        interest_rate_type=InterestRateType.FIXED, fixed_rate=Decimal(20),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2027, 1, 1),
        repayment_method=RepaymentMethod.BULLET, status=FacilityStatus.ACTIVE,
    )
    db_session.add(facility)
    await db_session.flush()

    repayment = FacilityRepayment(
        facility_id=facility.id, legal_entity_id=entity_a.id, currency_code="NGN",
        repayment_type=RepaymentType.PRINCIPAL, original_amount=Decimal(50000000),
        due_date=datetime.date(2026, 6, 3),
    )
    fee = FacilityFee(
        facility_id=facility.id, legal_entity_id=entity_a.id, fee_type=FeeType.PROCESSING,
        currency_code="NGN", amount=Decimal(2000000), due_date=datetime.date(2026, 6, 4),
        status=FeeStatus.DUE,
    )
    drawdown = FacilityDrawdown(
        facility_id=facility.id, legal_entity_id=entity_a.id, currency_code="NGN",
        drawdown_amount=Decimal(30000000), drawdown_date=datetime.date(2026, 6, 5),
        status=DrawdownStatus.APPROVED,
    )
    db_session.add_all([repayment, fee, drawdown])
    await db_session.flush()

    forecast = Forecast(
        group_id=group.id, legal_entity_id=entity_a.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 6, 1) + datetime.timedelta(days=13 * 7 - 1),
        value_basis=ForecastValueBasis.GROSS, reporting_currency_code="NGN",
        status=ForecastStatus.DRAFT,
    )
    db_session.add(forecast)
    await db_session.flush()

    forecast = await calculate_forecast(db_session, forecast)
    week1 = next(w for w in forecast.weeks if w.week_number == 1)

    assert week1.total_outflows >= Decimal("52000000.00")
    assert week1.total_inflows >= Decimal("30000000.00")

    from sqlalchemy import select

    from app.models.forecast import ForecastLine

    lines_result = await db_session.execute(
        select(ForecastLine).where(ForecastLine.forecast_id == forecast.id)
    )
    lines = list(lines_result.scalars().all())
    source_types = {ln.source_type.value for ln in lines}
    assert "FACILITY_REPAYMENT" in source_types
    assert "FACILITY_FEE" in source_types
    assert "FACILITY_DRAWDOWN" in source_types

    repayment_line = next(ln for ln in lines if ln.source_type.value == "FACILITY_REPAYMENT")
    assert repayment_line.source_id == str(repayment.id)
