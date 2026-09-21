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


async def _make_bank(db_session, name="Hardening Investment Bank"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


def _investment_payload(entity_id, bank_id, **overrides):
    payload = {
        "investment_reference": "INV-HARDEN-001", "investment_type_code": "FIXED_DEPOSIT",
        "legal_entity_id": str(entity_id), "institution_id": str(bank_id), "currency_code": "NGN",
        "principal_amount": "1000000000", "start_date": "2026-06-01", "maturity_date": "2026-09-01",
        "interest_rate": "18.0", "day_count_convention": "ACT_365",
    }
    payload.update(overrides)
    return payload


async def _create_and_approve(client, headers, entity_id, bank_id, **overrides):
    resp = await client.post(
        "/api/v1/investments", json=_investment_payload(entity_id, bank_id, **overrides), headers=headers,
    )
    assert resp.status_code == 201, resp.text
    inv_id = resp.json()["id"]
    for new_status in ("SUBMITTED", "UNDER_REVIEW", "APPROVED", "PLACEMENT_PENDING"):
        r = await client.patch(f"/api/v1/investments/{inv_id}/status", json={"new_status": new_status}, headers=headers)
        assert r.status_code == 200, r.text
    return inv_id


async def _make_scoped_user(db_session, *, scope_type, legal_entity_id=None, group_id=None, label):
    import uuid as uuid_module

    from app.core.security import hash_password
    from app.models.rbac import Permission, Role, TreasuryAction, TreasuryModule, UserRoleAssignment

    role = Role(name=f"{label}-role-{uuid_module.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for action in TreasuryAction:
        db_session.add(Permission(role_id=role.id, module=TreasuryModule.INVESTMENTS, action=action))
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


async def test_concurrent_partial_terminations_cannot_exceed_outstanding_principal(
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

    results = await asyncio.gather(
        *[
            client.post(
                f"/api/v1/investments/{inv_id}/terminate",
                json={"amount": "700000000", "termination_date": "2026-07-01"}, headers=headers,
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    status_codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert status_codes.count(200) == 1
    assert 409 in status_codes

    final = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    remaining = Decimal(final.json()["principal_amount"])
    assert remaining == Decimal("300000000.00")
    assert remaining >= 0


async def test_concurrent_placement_cannot_place_twice(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id)

    results = await asyncio.gather(
        *[
            client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    status_codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert status_codes.count(200) == 1

    txns = await client.get(f"/api/v1/investments/{inv_id}/transactions", headers=headers)
    placements = [t for t in txns.json() if t["transaction_type"] == "PLACEMENT"]
    assert len(placements) == 1


async def test_concurrent_rollover_creates_only_one_replacement_investment(
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

    async def _attempt(ref_suffix):
        return await client.post(
            f"/api/v1/investments/{inv_id}/rollover",
            json={"rollover_amount": "1000000000", "new_rate": "19.0", "new_start_date": "2026-09-01",
                  "new_maturity_date": "2026-12-01", "new_reference": f"INV-HARDEN-001-R{ref_suffix}"},
            headers=headers,
        )

    results = await asyncio.gather(_attempt(1), _attempt(2), return_exceptions=True)
    status_codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert status_codes.count(201) == 1
    # The loser sees either "amount exceeds outstanding principal" (409)
    # or "investment is no longer in a rollover-eligible status" (400) -
    # both are correct rejections; what matters is it's never a second 201.
    assert status_codes.count(201) == 1 and (400 in status_codes or 409 in status_codes)

    all_investments = await client.get(
        "/api/v1/investments", params={"legal_entity_id": str(entity_a.id)}, headers=headers,
    )
    rolled_investments = [i for i in all_investments.json() if i.get("previous_investment_id") == inv_id]
    assert len(rolled_investments) == 1


async def test_historical_investment_version_cannot_be_mutated_through_the_api(
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

    await client.post(
        f"/api/v1/investments/{inv_id}/rebook",
        json={"new_rate": "22.0", "additional_principal": "0", "reason": "Repricing"}, headers=headers,
    )

    versions_resp = await client.get(f"/api/v1/investments/{inv_id}/versions", headers=headers)
    versions = versions_resp.json()
    assert len(versions) == 2
    version_1_id = versions[0]["id"]

    patch_attempt = await client.patch(
        f"/api/v1/investment-versions/{version_1_id}", json={"terms": {"interest_rate": "1"}}, headers=headers,
    )
    assert patch_attempt.status_code == 404

    versions_after = await client.get(f"/api/v1/investments/{inv_id}/versions", headers=headers)
    assert versions_after.json()[0]["terms"]["interest_rate"] == "18.0"
    assert versions_after.json()[1]["terms"]["interest_rate"] == "22.0"


async def test_investment_maturity_appears_in_forecast_with_traceability_and_no_duplication(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, investment_types,
):
    from app.models.banking import Bank
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.models.investment import (
        DayCountConvention,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
    )
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="Forecast Investment Bank")
    db_session.add(bank)
    await db_session.flush()

    investment = Investment(
        investment_reference="INV-FCST-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_a.id, institution_id=bank.id, currency_code="NGN",
        principal_amount=Decimal(200000000), original_principal_amount=Decimal(200000000),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2026, 6, 5),
        tenor_days=155, interest_rate=Decimal(18), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.ACTIVE,
        expected_interest=Decimal("15287671.23"),
    )
    db_session.add(investment)
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

    principal_lines = await db_session.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.source_type == "INVESTMENT_MATURITY_PRINCIPAL",
            ForecastLine.source_id == str(investment.id),
        )
    )
    principal_rows = list(principal_lines.scalars().all())
    assert len(principal_rows) == 1
    assert principal_rows[0].reporting_amount == Decimal("200000000.00")

    interest_lines = await db_session.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.source_type == "INVESTMENT_MATURITY_INTEREST",
            ForecastLine.source_id == str(investment.id),
        )
    )
    interest_rows = list(interest_lines.scalars().all())
    assert len(interest_rows) == 1
    assert interest_rows[0].reporting_amount == Decimal("15287671.23")


async def test_entity_a_cannot_see_or_modify_entity_b_investment(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, investment_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="usera-inv",
    )
    await db_session.commit()
    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}

    create_b = await client.post(
        "/api/v1/investments", json=_investment_payload(entity_b.id, bank.id), headers=headers_a,
    )
    assert create_b.status_code == 403

    from app.models.investment import (
        DayCountConvention,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
    )

    investment_b = Investment(
        investment_reference="INV-B-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_b.id, institution_id=bank.id, currency_code="USD",
        principal_amount=Decimal(1000000), original_principal_amount=Decimal(1000000),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2026, 6, 1),
        tenor_days=151, interest_rate=Decimal(5), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.ACTIVE,
    )
    db_session.add(investment_b)
    await db_session.commit()

    list_resp = await client.get("/api/v1/investments", headers=headers_a)
    assert str(investment_b.id) not in {i["id"] for i in list_resp.json()}

    detail_resp = await client.get(f"/api/v1/investments/{investment_b.id}", headers=headers_a)
    assert detail_resp.status_code == 403

    terminate_resp = await client.post(
        f"/api/v1/investments/{investment_b.id}/terminate",
        json={"amount": "1000", "termination_date": "2026-02-01"}, headers=headers_a,
    )
    assert terminate_resp.status_code == 403

    liquidity_resp = await client.get(
        "/api/v1/investments-reports/liquidity", params={"legal_entity_id": str(entity_b.id)}, headers=headers_a,
    )
    assert liquidity_resp.status_code == 403


async def test_cross_group_investment_access_is_blocked(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, investment_types,
):
    from app.models.entity import Group, LegalEntity
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)

    group2 = Group(name="Inv Second Group", code="INVGRP2", reporting_currency_code="USD")
    db_session.add(group2)
    await db_session.flush()
    entity_c = LegalEntity(
        group_id=group2.id, name="Inv Entity C", code="INVENTC", functional_currency_code="USD", country="US",
    )
    db_session.add(entity_c)
    await db_session.flush()

    from app.models.investment import (
        DayCountConvention,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
    )

    investment_c = Investment(
        investment_reference="INV-C-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_c.id, institution_id=bank.id, currency_code="USD",
        principal_amount=Decimal(2000000), original_principal_amount=Decimal(2000000),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2026, 6, 1),
        tenor_days=151, interest_rate=Decimal(5), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.ACTIVE,
    )
    db_session.add(investment_c)

    group_user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group.id, label="invgroupuser",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, group_user)}"}

    list_resp = await client.get("/api/v1/investments", headers=headers)
    assert str(investment_c.id) not in {i["id"] for i in list_resp.json()}
    detail_resp = await client.get(f"/api/v1/investments/{investment_c.id}", headers=headers)
    assert detail_resp.status_code == 403


async def test_investment_master_excel_import_rejects_unauthorized_entity_rows(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, investment_types,
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
    bank = Bank(name="Investment Excel Auth Bank")
    db_session.add(bank)

    role = Role(name=f"usera-ix-role-{uuid_module.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for action in (TreasuryAction.VIEW, TreasuryAction.UPLOAD, TreasuryAction.IMPORT):
        db_session.add(Permission(role_id=role.id, module=TreasuryModule.EXCEL_DATA_HUB, action=action))
    db_session.add(Permission(role_id=role.id, module=TreasuryModule.INVESTMENTS, action=TreasuryAction.VIEW))
    user_a = User(
        email=f"usera-ix-{uuid_module.uuid4().hex[:8]}@treasuryos.example.com",
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
        ["Investment Reference", "Entity", "Institution", "Investment Type", "Currency",
         "Principal Amount", "Start Date", "Maturity Date", "Interest Rate"],
        [
            ["INV-XA-001", "Entity A", "Investment Excel Auth Bank", "FIXED_DEPOSIT", "NGN",
             "100000000", "2026-01-01", "2026-06-01", "18.0"],
            ["INV-XB-001", "Entity B", "Investment Excel Auth Bank", "FIXED_DEPOSIT", "USD",
             "200000", "2026-01-01", "2026-06-01", "5.0"],
        ],
    )
    upload_resp = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "INVESTMENT_MASTER", "template_version": "1",
              "legal_entity_id": str(entity_a.id)},
        files={"file": ("investments.xlsx", file_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )
    assert upload_resp.status_code == 201
    batch = upload_resp.json()
    assert batch["valid_rows"] == 2

    confirm_resp = await client.post(f"/api/v1/excel/imports/{batch['id']}/confirm", headers=headers)
    assert confirm_resp.status_code == 200
    result = confirm_resp.json()
    assert result["imported_rows"] == 1
    assert result["skipped_rows"] == 1

    investments_resp = await client.get(
        "/api/v1/investments", params={"legal_entity_id": str(entity_a.id)}, headers=headers,
    )
    references = {i["investment_reference"] for i in investments_resp.json()}
    assert "INV-XA-001" in references
    assert "INV-XB-001" not in references


async def test_concentration_breach_status_against_configured_limit(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from app.models.investment import InvestmentConcentrationLimit

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    db_session.add(InvestmentConcentrationLimit(
        name="Institution Cap", institution_id=bank.id, limit_amount=Decimal(500000000),
        limit_currency_code="NGN", warning_threshold_pct=Decimal(80),
    ))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, principal_amount="600000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    concentration = await client.get(
        "/api/v1/investments-reports/concentration", params={"legal_entity_id": str(entity_a.id)}, headers=headers,
    )
    institution_rows = [r for r in concentration.json() if r["dimension"] == "INSTITUTION"]
    assert len(institution_rows) == 1
    assert institution_rows[0]["status"] == "BREACH"
    assert institution_rows[0]["current_amount"] == "600000000.00"
    assert institution_rows[0]["limit_amount"] == "500000000.00"


async def test_investment_preserves_its_own_currency_never_silently_converted(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    usd_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, investment_reference="INV-USD-001", currency_code="USD",
    )
    await client.post(f"/api/v1/investments/{usd_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    ngn_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, investment_reference="INV-NGN-001", currency_code="NGN",
    )
    await client.post(f"/api/v1/investments/{ngn_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    usd_check = await client.get(f"/api/v1/investments/{usd_id}", headers=headers)
    assert usd_check.json()["currency_code"] == "USD"
    ngn_check = await client.get(f"/api/v1/investments/{ngn_id}", headers=headers)
    assert ngn_check.json()["currency_code"] == "NGN"

    liquidity = await client.get(
        "/api/v1/investments-reports/liquidity", params={"legal_entity_id": str(entity_a.id)}, headers=headers,
    )
    assert "USD" in liquidity.json()["by_currency"]
    assert "NGN" in liquidity.json()["by_currency"]
    # Both are tracked as separate currency keys, never merged into one
    # combined total - this is the property under test, not that their
    # amounts happen to differ (they may coincidentally be equal).
    assert set(liquidity.json()["by_currency"].keys()) >= {"USD", "NGN"}
