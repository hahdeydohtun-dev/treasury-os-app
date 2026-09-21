import asyncio
import datetime
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


async def _make_bank(db_session, name="Hardening Test Lender"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


def _facility_payload(entity_id, lender_id, **overrides):
    payload = {
        "facility_reference": "FAC-HARDEN-001", "facility_name": "Hardening Test Facility",
        "facility_type_code": "REVOLVING_CREDIT", "commitment_type": "COMMITTED",
        "lender_id": str(lender_id), "legal_entity_id": str(entity_id), "currency_code": "NGN",
        "approved_limit": "1000000000", "committed_limit": "1000000000",
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


async def test_executing_a_drawdown_twice_does_not_double_the_drawn_amount(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    drawdown_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/drawdowns",
        json={"legal_entity_id": str(entity_a.id), "currency_code": "NGN",
              "drawdown_amount": "100000000", "drawdown_date": "2026-02-01"},
        headers=headers,
    )
    drawdown_id = drawdown_resp.json()["id"]
    await client.post(f"/api/v1/drawdowns/{drawdown_id}/approve", headers=headers)

    first_execute = await client.post(f"/api/v1/drawdowns/{drawdown_id}/execute", headers=headers)
    assert first_execute.status_code == 200
    assert first_execute.json()["status"] == "EXECUTED"

    second_execute = await client.post(f"/api/v1/drawdowns/{drawdown_id}/execute", headers=headers)
    assert second_execute.status_code == 400

    utilization = await client.get(f"/api/v1/facilities/{facility_id}/utilization", headers=headers)
    assert utilization.json()["drawn_amount"] == "100000000.00"


async def test_paying_a_repayment_twice_in_full_does_not_reduce_principal_twice(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    repayment_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/repayments",
        json={"legal_entity_id": str(entity_a.id), "currency_code": "NGN",
              "repayment_type": "PRINCIPAL", "original_amount": "50000000", "due_date": "2026-06-01"},
        headers=headers,
    )
    repayment_id = repayment_resp.json()["id"]

    first_pay = await client.post(
        f"/api/v1/repayments/{repayment_id}/pay",
        json={"paid_amount": "50000000", "actual_payment_date": "2026-06-01"}, headers=headers,
    )
    assert first_pay.status_code == 200
    assert first_pay.json()["status"] == "PAID"

    second_pay = await client.post(
        f"/api/v1/repayments/{repayment_id}/pay",
        json={"paid_amount": "50000000", "actual_payment_date": "2026-06-02"}, headers=headers,
    )
    assert second_pay.status_code == 400

    final = await client.get(f"/api/v1/repayments/{repayment_id}", headers=headers)
    assert final.json()["paid_amount"] == "50000000.00"


async def test_repayment_overpayment_is_rejected_and_does_not_mutate_balance(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    repayment_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/repayments",
        json={"legal_entity_id": str(entity_a.id), "currency_code": "NGN",
              "repayment_type": "PRINCIPAL", "original_amount": "10000000", "due_date": "2026-06-01"},
        headers=headers,
    )
    repayment_id = repayment_resp.json()["id"]

    overpay = await client.post(
        f"/api/v1/repayments/{repayment_id}/pay",
        json={"paid_amount": "20000000", "actual_payment_date": "2026-06-01"}, headers=headers,
    )
    assert overpay.status_code == 400

    unchanged = await client.get(f"/api/v1/repayments/{repayment_id}", headers=headers)
    assert unchanged.json()["paid_amount"] == "0.00"
    assert unchanged.json()["status"] == "SCHEDULED"


async def test_funding_action_illegal_execute_transition_does_not_mutate_status(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities,
):
    group, entity_a, _ = demo_group_and_entities
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/funding/actions",
        json={"action_type": "PROPOSE_DRAWDOWN", "legal_entity_id": str(entity_a.id),
              "amount": "1000000", "currency_code": "NGN"},
        headers=headers,
    )
    action_id = create_resp.json()["id"]

    bad = await client.post(f"/api/v1/funding/actions/{action_id}/execute", headers=headers)
    assert bad.status_code == 400
    still_draft = await client.get(f"/api/v1/funding/actions/{action_id}", headers=headers)
    assert still_draft.json()["status"] == "DRAFT"


async def test_concurrent_drawdown_execution_cannot_overdraw_the_facility(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)

    drawdown_ids = []
    for _ in range(2):
        resp = await client.post(
            f"/api/v1/facilities/{facility_id}/drawdowns",
            json={"legal_entity_id": str(entity_a.id), "currency_code": "NGN",
                  "drawdown_amount": "800000000", "drawdown_date": "2026-02-01"},
            headers=headers,
        )
        assert resp.status_code == 201
        drawdown_id = resp.json()["id"]
        await client.post(f"/api/v1/drawdowns/{drawdown_id}/approve", headers=headers)
        drawdown_ids.append(drawdown_id)

    results = await asyncio.gather(
        *[
            client.post(f"/api/v1/drawdowns/{d_id}/execute", headers=headers)
            for d_id in drawdown_ids
        ],
        return_exceptions=True,
    )

    status_codes = sorted(
        r.status_code for r in results if not isinstance(r, Exception)
    )
    assert status_codes.count(200) == 1
    assert 200 in status_codes and 409 in status_codes

    utilization = await client.get(f"/api/v1/facilities/{facility_id}/utilization", headers=headers)
    drawn = Decimal(utilization.json()["drawn_amount"])
    assert drawn == Decimal("800000000.00")
    assert drawn <= Decimal("1000000000.00")


async def test_concurrent_drawdown_execution_against_the_same_sub_limit_is_protected(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    facility_id = await _create_active_facility(client, headers, entity_a.id, bank.id)
    sub_limit_resp = await client.post(
        f"/api/v1/facilities/{facility_id}/sub-limits",
        json={"name": "Race Tranche", "limit_amount": "100000000"}, headers=headers,
    )
    sub_limit_id = sub_limit_resp.json()["id"]

    drawdown_ids = []
    for _ in range(2):
        resp = await client.post(
            f"/api/v1/facilities/{facility_id}/drawdowns",
            json={"legal_entity_id": str(entity_a.id), "sub_limit_id": sub_limit_id,
                  "currency_code": "NGN", "drawdown_amount": "80000000", "drawdown_date": "2026-02-01"},
            headers=headers,
        )
        assert resp.status_code == 201
        drawdown_id = resp.json()["id"]
        await client.post(f"/api/v1/drawdowns/{drawdown_id}/approve", headers=headers)
        drawdown_ids.append(drawdown_id)

    results = await asyncio.gather(
        *[
            client.post(f"/api/v1/drawdowns/{d_id}/execute", headers=headers)
            for d_id in drawdown_ids
        ],
        return_exceptions=True,
    )
    status_codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert status_codes.count(200) == 1
    assert 409 in status_codes

    sub_limits = await client.get(f"/api/v1/facilities/{facility_id}/sub-limits", headers=headers)
    drawn = Decimal(sub_limits.json()[0]["drawn_amount"])
    assert drawn == Decimal("80000000.00")
    assert drawn <= Decimal("100000000.00")


async def test_historical_facility_version_cannot_be_mutated_through_the_api(
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

    await client.patch(
        f"/api/v1/facilities/{facility_id}",
        json={"committed_limit": "1500000000", "fixed_rate": "18.0", "change_reason": "Repricing"},
        headers=headers,
    )
    await client.patch(
        f"/api/v1/facilities/{facility_id}",
        json={"committed_limit": "2000000000", "change_reason": "Second increase"},
        headers=headers,
    )

    versions_resp = await client.get(f"/api/v1/facilities/{facility_id}/versions", headers=headers)
    versions = versions_resp.json()
    assert len(versions) == 3
    version_1_id = versions[0]["id"]

    patch_attempt = await client.patch(
        f"/api/v1/facility-versions/{version_1_id}", json={"terms": {"committed_limit": "1"}},
        headers=headers,
    )
    assert patch_attempt.status_code == 404

    versions_after = await client.get(f"/api/v1/facilities/{facility_id}/versions", headers=headers)
    assert versions_after.json()[0]["terms"]["committed_limit"] == "1000000000"
    assert versions_after.json()[1]["terms"]["committed_limit"] == "1500000000"
    assert versions_after.json()[2]["terms"]["committed_limit"] == "2000000000"


async def test_historical_interest_calculation_remains_reproducible_after_repricing(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, facility_types,
):
    from app.models.facility import DayCountConvention
    from app.services.facility_engine import calculate_interest

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/facilities", json=_facility_payload(entity_a.id, bank.id, fixed_rate="20.0"),
        headers=headers,
    )
    facility_id = create_resp.json()["id"]

    old_rate = Decimal("20.0")
    principal = Decimal(100000000)
    start = datetime.date(2026, 1, 1)
    end = datetime.date(2026, 4, 1)
    interest_before = calculate_interest(principal, old_rate, start, end, DayCountConvention.ACT_365)

    await client.patch(
        f"/api/v1/facilities/{facility_id}",
        json={"fixed_rate": "35.0", "change_reason": "Major repricing"}, headers=headers,
    )

    versions_resp = await client.get(f"/api/v1/facilities/{facility_id}/versions", headers=headers)
    v1_rate = Decimal(versions_resp.json()[0]["terms"]["fixed_rate"])
    interest_after = calculate_interest(principal, v1_rate, start, end, DayCountConvention.ACT_365)

    assert interest_before == interest_after
    assert v1_rate == old_rate


async def test_recalculating_a_forecast_does_not_duplicate_facility_lines(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, facility_types,
):
    from app.models.banking import Bank
    from app.models.facility import (
        CommitmentType,
        Facility,
        FacilityRepayment,
        FacilityStatus,
        InterestRateType,
        RepaymentMethod,
        RepaymentType,
    )
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="Dup Protection Bank")
    db_session.add(bank)
    await db_session.flush()

    facility = Facility(
        facility_reference="FAC-DUP-001", facility_name="Dup Protection Facility",
        facility_type_code="TERM_LOAN", commitment_type=CommitmentType.COMMITTED,
        lender_id=bank.id, legal_entity_id=entity_a.id, currency_code="NGN",
        approved_limit=Decimal(500000000), committed_limit=Decimal(500000000),
        current_drawn_amount=Decimal(100000000), interest_rate_type=InterestRateType.FIXED,
        fixed_rate=Decimal(20), start_date=datetime.date(2026, 1, 1),
        maturity_date=datetime.date(2027, 1, 1), repayment_method=RepaymentMethod.BULLET,
        status=FacilityStatus.ACTIVE,
    )
    db_session.add(facility)
    await db_session.flush()

    repayment = FacilityRepayment(
        facility_id=facility.id, legal_entity_id=entity_a.id, currency_code="NGN",
        repayment_type=RepaymentType.PRINCIPAL, original_amount=Decimal(30000000),
        due_date=datetime.date(2026, 6, 3),
    )
    db_session.add(repayment)
    await db_session.flush()

    forecast = Forecast(
        group_id=group.id, legal_entity_id=entity_a.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 6, 1) + datetime.timedelta(days=13 * 7 - 1),
        value_basis=ForecastValueBasis.GROSS, reporting_currency_code="NGN",
        status=ForecastStatus.DRAFT,
    )
    db_session.add(forecast)
    await db_session.flush()

    from sqlalchemy import select

    await calculate_forecast(db_session, forecast)
    await calculate_forecast(db_session, forecast)
    await calculate_forecast(db_session, forecast)

    lines_result = await db_session.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.source_type == "FACILITY_REPAYMENT",
            ForecastLine.source_id == str(repayment.id),
        )
    )
    matching_lines = list(lines_result.scalars().all())
    assert len(matching_lines) == 1


async def test_entity_a_cannot_view_entity_b_funding_forecast_impact(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, forecast_categories,
):
    from app.models.forecast import Forecast, ForecastStatus, ForecastValueBasis
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, entity_b = demo_group_and_entities
    forecast_b = Forecast(
        group_id=group.id, legal_entity_id=entity_b.id, forecast_start_date=datetime.date(2026, 6, 1),
        forecast_end_date=datetime.date(2026, 6, 1) + datetime.timedelta(days=13 * 7 - 1),
        value_basis=ForecastValueBasis.GROSS, reporting_currency_code="USD",
        status=ForecastStatus.DRAFT,
    )
    db_session.add(forecast_b)
    await db_session.flush()
    await calculate_forecast(db_session, forecast_b)
    await db_session.commit()

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

    role = Role(name=f"usera-fi-role-{uuid_module.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for action in (TreasuryAction.VIEW,):
        db_session.add(Permission(role_id=role.id, module=TreasuryModule.FACILITIES, action=action))
    user_a = User(
        email=f"usera-fi-{uuid_module.uuid4().hex[:8]}@treasuryos.example.com",
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
    resp = await client.get(
        "/api/v1/funding/forecast-impact", params={"forecast_id": str(forecast_b.id)}, headers=headers_a,
    )
    assert resp.status_code == 403


async def test_facility_master_excel_import_rejects_unauthorized_entity_rows(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, facility_types,
):
    import uuid as uuid_module

    from app.core.security import hash_password
    from app.models.banking import Bank
    from app.models.rbac import (
        EntityScopeType,
        Permission,
        Role,
        TreasuryAction,
        TreasuryModule,
        UserRoleAssignment,
    )
    from tests.conftest import make_xlsx_bytes

    group, entity_a, entity_b = demo_group_and_entities
    bank = Bank(name="Facility Excel Auth Bank")
    db_session.add(bank)

    role = Role(name=f"usera-fx-role-{uuid_module.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for action in (TreasuryAction.VIEW, TreasuryAction.UPLOAD, TreasuryAction.IMPORT):
        db_session.add(Permission(role_id=role.id, module=TreasuryModule.EXCEL_DATA_HUB, action=action))
    db_session.add(Permission(role_id=role.id, module=TreasuryModule.FACILITIES, action=TreasuryAction.VIEW))
    user_a = User(
        email=f"usera-fx-{uuid_module.uuid4().hex[:8]}@treasuryos.example.com",
        full_name="User A", hashed_password=hash_password("Password123!"), is_superuser=False,
    )
    db_session.add(user_a)
    await db_session.flush()
    db_session.add(UserRoleAssignment(
        user_id=user_a.id, role_id=role.id, scope_type=EntityScopeType.ENTITY,
        legal_entity_id=entity_a.id,
    ))
    await db_session.commit()

    headers = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    file_bytes = make_xlsx_bytes(
        ["Facility Reference", "Entity", "Lender", "Facility Type", "Commitment Type",
         "Currency", "Committed Limit", "Maturity Date"],
        [
            ["FAC-XA-001", "Entity A", "Facility Excel Auth Bank", "TERM_LOAN", "COMMITTED",
             "NGN", "100000000", "2027-01-01"],
            ["FAC-XB-001", "Entity B", "Facility Excel Auth Bank", "TERM_LOAN", "COMMITTED",
             "USD", "200000", "2027-01-01"],
        ],
    )
    upload_resp = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "FACILITY_MASTER", "template_version": "1",
              "legal_entity_id": str(entity_a.id)},
        files={"file": ("facilities.xlsx", file_bytes,
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

    facilities_resp = await client.get(
        "/api/v1/facilities", params={"legal_entity_id": str(entity_a.id)}, headers=headers,
    )
    references = {f["facility_reference"] for f in facilities_resp.json()}
    assert "FAC-XA-001" in references
    assert "FAC-XB-001" not in references


async def test_facility_with_no_scheduled_repayment_produces_no_maturity_forecast_line(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, facility_types,
):
    from app.models.banking import Bank
    from app.models.facility import (
        CommitmentType,
        Facility,
        FacilityStatus,
        InterestRateType,
        RepaymentMethod,
    )
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="Maturity Test Bank")
    db_session.add(bank)
    await db_session.flush()

    maturity_date = datetime.date(2026, 6, 10)
    facility = Facility(
        facility_reference="FAC-MATURITY-001", facility_name="Maturity Test Facility",
        facility_type_code="TERM_LOAN", commitment_type=CommitmentType.COMMITTED,
        lender_id=bank.id, legal_entity_id=entity_a.id, currency_code="NGN",
        approved_limit=Decimal(200000000), committed_limit=Decimal(200000000),
        current_drawn_amount=Decimal(200000000), interest_rate_type=InterestRateType.FIXED,
        fixed_rate=Decimal(20), start_date=datetime.date(2026, 1, 1), maturity_date=maturity_date,
        repayment_method=RepaymentMethod.BULLET, status=FacilityStatus.ACTIVE,
    )
    db_session.add(facility)
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

    from sqlalchemy import select
    lines_result = await db_session.execute(
        select(ForecastLine).where(ForecastLine.forecast_id == forecast.id)
    )
    facility_lines = [
        ln for ln in lines_result.scalars().all()
        if ln.source_type.value.startswith("FACILITY_")
    ]
    assert facility_lines == []
