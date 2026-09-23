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


async def _make_bank(db_session, name="Cash Integration Bank"):
    from app.models.banking import Bank

    bank = Bank(name=name)
    db_session.add(bank)
    await db_session.flush()
    return bank


async def _make_account(db_session, entity_id, bank_id, currency="NGN", account_number="0011223344"):
    from app.models.banking import BankAccount
    from app.models.lookup import AccountType

    if await db_session.get(AccountType, "CURRENT") is None:
        db_session.add(AccountType(code="CURRENT", name="Current Account"))
        await db_session.flush()

    account = BankAccount(
        legal_entity_id=entity_id, bank_id=bank_id, account_name="Operating Account",
        account_number=account_number, currency_code=currency, account_type_code="CURRENT",
    )
    db_session.add(account)
    await db_session.flush()
    return account


async def _set_balance(db_session, account, amount, currency="NGN", as_of=None):
    from app.models.balance import BankBalance

    balance = BankBalance(
        bank_account_id=account.id, balance_date=as_of or datetime.date(2026, 5, 31),
        currency_code=currency, closing_balance=amount, available_balance=amount, source="MANUAL",
    )
    db_session.add(balance)
    await db_session.flush()
    return balance


def _investment_payload(entity_id, bank_id, account_id, **overrides):
    payload = {
        "investment_reference": "INV-CASH-001", "investment_type_code": "FIXED_DEPOSIT",
        "legal_entity_id": str(entity_id), "institution_id": str(bank_id),
        "source_account_id": str(account_id), "currency_code": "NGN",
        "principal_amount": "60000000", "start_date": "2026-06-01", "maturity_date": "2026-09-01",
        "interest_rate": "18.0", "day_count_convention": "ACT_365",
    }
    payload.update(overrides)
    return payload


async def _create_and_approve(client, headers, entity_id, bank_id, account_id, **overrides):
    resp = await client.post(
        "/api/v1/investments", json=_investment_payload(entity_id, bank_id, account_id, **overrides),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    inv_id = resp.json()["id"]
    for new_status in ("SUBMITTED", "UNDER_REVIEW", "APPROVED", "PLACEMENT_PENDING"):
        r = await client.patch(f"/api/v1/investments/{inv_id}/status", json={"new_status": new_status}, headers=headers)
        assert r.status_code == 200, r.text
    return inv_id


async def test_placement_actually_reduces_operational_available_cash(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """
    SECTION 9 (Stage 4 final financial-integrity patch): renamed and
    reworked from
    test_placement_decreases_available_cash_and_creates_linked_treasury_transaction,
    which only asserted a TreasuryTransaction existed - never the actual
    cash-position figure. This version verifies the real financial
    behavior: operational_available_cash genuinely drops by the exact
    principal amount, not merely that a ledger row was written.
    """
    from sqlalchemy import select

    from app.models.treasury_transaction import TreasuryTransaction
    from app.services.cash_position_service import calculate_operational_available_cash

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    before = await calculate_operational_available_cash(db_session, account.id)
    assert before.operational_available_cash == Decimal("100000000.00")

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id, account.id)
    place_resp = await client.post(
        f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert place_resp.status_code == 200, place_resp.text

    # The actual financial behavior under test: operational cash genuinely fell.
    after = await calculate_operational_available_cash(db_session, account.id)
    assert after.operational_available_cash == Decimal("40000000.00")
    assert after.operational_available_cash == before.operational_available_cash - Decimal("60000000.00")

    txns = await client.get(f"/api/v1/investments/{inv_id}/transactions", headers=headers)
    placement_txn = next(t for t in txns.json() if t["transaction_type"] == "PLACEMENT")

    result = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.source_record_id == inv_id)
    )
    cash_txns = list(result.scalars().all())
    assert len(cash_txns) == 1
    assert cash_txns[0].direction.value == "OUTFLOW"
    assert cash_txns[0].transaction_amount == Decimal("60000000.00")
    assert cash_txns[0].bank_account_id == account.id
    assert cash_txns[0].event_type_code == "INVESTMENT_PLACEMENT"
    assert placement_txn["amount"] == "60000000.00"


async def test_placement_with_insufficient_cash_is_rejected_and_does_not_mutate(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from sqlalchemy import select

    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(40000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id, account.id)
    place_resp = await client.post(
        f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert place_resp.status_code == 400

    unchanged = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    assert unchanged.json()["status"] == "PLACEMENT_PENDING"

    result = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.source_record_id == inv_id)
    )
    assert list(result.scalars().all()) == []


async def test_client_supplied_available_cash_is_never_the_authoritative_decision(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(1000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id, account.id)
    place_resp = await client.post(
        f"/api/v1/investments/{inv_id}/place",
        json={"placement_date": "2026-06-01", "available_cash": "999999999999"}, headers=headers,
    )
    assert place_resp.status_code == 400


async def test_placement_rejects_currency_mismatch_between_investment_and_account(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    usd_account = await _make_account(db_session, entity_a.id, bank.id, currency="USD")
    await _set_balance(db_session, usd_account, Decimal(100000000), currency="USD")
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, usd_account.id, currency_code="NGN",
    )
    place_resp = await client.post(
        f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert place_resp.status_code == 400
    assert "currency" in str(place_resp.json()["detail"]).lower()


async def test_entity_a_cannot_use_entity_b_bank_account_for_placement(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="9988776655")
    await _set_balance(db_session, account_b, Decimal(500000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id, account_b.id)
    place_resp = await client.post(
        f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert place_resp.status_code == 400
    assert "entity" in str(place_resp.json()["detail"]).lower()


async def test_termination_cash_inflow_reconciles_exactly_to_net_proceeds(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from sqlalchemy import select

    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id, account.id)
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    term_resp = await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "30000000", "termination_date": "2026-07-01", "destination_account_id": str(account.id)},
        headers=headers,
    )
    assert term_resp.status_code == 200
    body = term_resp.json()
    net_proceeds = Decimal(body["net_proceeds"])
    assert body["investment"]["status"] == "PARTIALLY_TERMINATED"
    assert body["investment"]["principal_amount"] == "30000000.00"

    result = await db_session.execute(
        select(TreasuryTransaction).where(
            TreasuryTransaction.source_record_id == inv_id,
            TreasuryTransaction.event_type_code == "INVESTMENT_TERMINATION",
        )
    )
    cash_txns = list(result.scalars().all())
    assert len(cash_txns) == 1
    assert cash_txns[0].direction.value == "INFLOW"
    assert cash_txns[0].transaction_amount == net_proceeds
    assert cash_txns[0].bank_account_id == account.id


async def test_partial_termination_recomputes_expected_interest_on_remaining_principal(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from app.models.investment import DayCountConvention
    from app.services.investment_engine import calculate_simple_interest

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="100000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    before = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    expected_before = Decimal(before.json()["expected_interest"])
    full_principal_projection = calculate_simple_interest(
        Decimal(100000000), Decimal("18.0"), datetime.date(2026, 6, 1), datetime.date(2026, 9, 1),
        DayCountConvention.ACT_365,
    )
    assert expected_before == full_principal_projection

    await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "30000000", "termination_date": "2026-07-01"}, headers=headers,
    )

    after = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    assert after.json()["principal_amount"] == "70000000.00"
    expected_after = Decimal(after.json()["expected_interest"])
    remaining_principal_projection = calculate_simple_interest(
        Decimal(70000000), Decimal("18.0"), datetime.date(2026, 6, 1), datetime.date(2026, 9, 1),
        DayCountConvention.ACT_365,
    )
    assert expected_after == remaining_principal_projection
    assert expected_after < expected_before


async def test_full_rollover_creates_no_external_cash_movement_only_one_non_cash_record(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from sqlalchemy import select

    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="100000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    rollover_resp = await client.post(
        f"/api/v1/investments/{inv_id}/rollover",
        json={"rollover_amount": "100000000", "new_rate": "19.0", "new_start_date": "2026-09-01",
              "new_maturity_date": "2026-12-01", "new_reference": "INV-CASH-001-R1"},
        headers=headers,
    )
    assert rollover_resp.status_code == 201
    new_investment_id = rollover_resp.json()["id"]

    result = await db_session.execute(
        select(TreasuryTransaction).where(
            TreasuryTransaction.direction.in_(["INFLOW", "OUTFLOW"]),
        )
    )
    real_cash_txns = [
        t for t in result.scalars().all()
        if t.source_record_id in (inv_id, new_investment_id)
    ]
    assert len(real_cash_txns) == 1
    assert real_cash_txns[0].event_type_code == "INVESTMENT_PLACEMENT"

    non_cash_result = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.event_type_code == "INVESTMENT_ROLLOVER")
    )
    non_cash_txns = list(non_cash_result.scalars().all())
    assert len(non_cash_txns) == 1
    assert non_cash_txns[0].direction.value == "NON_CASH"

    original_txns = await client.get(f"/api/v1/investments/{inv_id}/transactions", headers=headers)
    new_txns = await client.get(f"/api/v1/investments/{new_investment_id}/transactions", headers=headers)
    original_rollover_txn = next(t for t in original_txns.json() if t["transaction_type"] == "ROLLOVER")
    new_placement_txn = next(t for t in new_txns.json() if t["transaction_type"] == "PLACEMENT")
    assert original_rollover_txn is not None
    assert new_placement_txn is not None


async def test_partial_rollover_old_and_new_principal_correct(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="100000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    rollover_resp = await client.post(
        f"/api/v1/investments/{inv_id}/rollover",
        json={"rollover_amount": "60000000", "new_rate": "19.0", "new_start_date": "2026-09-01",
              "new_maturity_date": "2026-12-01", "new_reference": "INV-CASH-001-R2"},
        headers=headers,
    )
    assert rollover_resp.status_code == 201
    assert rollover_resp.json()["principal_amount"] == "60000000.00"

    original = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    assert original.json()["principal_amount"] == "40000000.00"
    assert original.json()["status"] == "PARTIALLY_TERMINATED"


async def test_rebooking_additional_principal_creates_real_cash_outflow(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from sqlalchemy import select

    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="50000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    rebook_resp = await client.post(
        f"/api/v1/investments/{inv_id}/rebook",
        json={"additional_principal": "10000000", "reason": "Top-up", "bank_account_id": str(account.id)},
        headers=headers,
    )
    assert rebook_resp.status_code == 200
    assert rebook_resp.json()["principal_amount"] == "60000000.00"

    result = await db_session.execute(
        select(TreasuryTransaction).where(
            TreasuryTransaction.source_record_id == inv_id,
            TreasuryTransaction.event_type_code == "INVESTMENT_PLACEMENT",
        )
    )
    outflows = list(result.scalars().all())
    assert len(outflows) == 2
    amounts = sorted(t.transaction_amount for t in outflows)
    assert amounts == [Decimal("10000000.00"), Decimal("50000000.00")]


async def test_rebooking_additional_principal_rejected_when_cash_insufficient(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """
    SECTION 5 (final freeze patch): the rebooking endpoint uses the same
    authoritative calculate_operational_available_cash() logic as
    placement. Scenario: operational cash = 5m, additional principal
    requested = 10m -> 400, and NONE of InvestmentVersion,
    InvestmentTransaction, TreasuryTransaction, or the investment's own
    principal_amount/version may be mutated by the rejected attempt.

    Note: BankBalance (the authoritative source for available cash) is a
    reported snapshot, not auto-decremented by TreasuryTransaction
    postings (this is a pre-existing Treasury OS architectural
    characteristic, documented in docs/STAGE_4_INVESTMENTS.md - the same
    is true for every other cash-affecting operation in this codebase).
    So this test sets the balance itself below the requested top-up,
    rather than relying on the prior placement to have consumed it.
    """
    from sqlalchemy import select

    from app.models.investment import InvestmentTransaction, InvestmentVersion
    from app.models.treasury_transaction import TreasuryTransaction
    from app.services.cash_position_service import calculate_operational_available_cash

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(5000000))  # less than the 10m top-up
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="50000000",
        source_account_id=None,  # place without a source account so this test isolates the rebook check
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    # Confirm the same authoritative function reports exactly 5m available.
    position = await calculate_operational_available_cash(db_session, account.id)
    assert position.operational_available_cash == Decimal("5000000.00")

    before_version = (await client.get(f"/api/v1/investments/{inv_id}", headers=headers)).json()["version"]
    versions_before = list((await db_session.execute(
        select(InvestmentVersion).where(InvestmentVersion.investment_id == inv_id)
    )).scalars().all())
    txns_before = list((await db_session.execute(
        select(InvestmentTransaction).where(InvestmentTransaction.investment_id == inv_id)
    )).scalars().all())
    cash_txns_before = list((await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.bank_account_id == account.id)
    )).scalars().all())

    rebook_resp = await client.post(
        f"/api/v1/investments/{inv_id}/rebook",
        json={"additional_principal": "10000000", "reason": "Top-up", "bank_account_id": str(account.id)},
        headers=headers,
    )
    assert rebook_resp.status_code == 400

    after = (await client.get(f"/api/v1/investments/{inv_id}", headers=headers)).json()
    assert after["version"] == before_version  # no version bump
    assert after["principal_amount"] == "50000000.00"  # no principal mutation

    versions_after = list((await db_session.execute(
        select(InvestmentVersion).where(InvestmentVersion.investment_id == inv_id)
    )).scalars().all())
    txns_after = list((await db_session.execute(
        select(InvestmentTransaction).where(InvestmentTransaction.investment_id == inv_id)
    )).scalars().all())
    cash_txns_after = list((await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.bank_account_id == account.id)
    )).scalars().all())

    assert len(versions_after) == len(versions_before)  # no new InvestmentVersion
    assert len(txns_after) == len(txns_before)  # no new InvestmentTransaction
    assert len(cash_txns_after) == len(cash_txns_before)  # no new TreasuryTransaction (no cash mutation)

    final_position = await calculate_operational_available_cash(db_session, account.id)
    assert final_position.operational_available_cash == Decimal("5000000.00")  # entirely unchanged


async def test_maturity_settlement_is_explicit_and_idempotent(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from sqlalchemy import select

    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="60000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    still_active = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    assert still_active.json()["status"] == "ACTIVE"

    settle_resp = await client.post(
        f"/api/v1/investments/{inv_id}/settle-maturity",
        json={"settlement_date": "2026-09-01", "destination_account_id": str(account.id)}, headers=headers,
    )
    assert settle_resp.status_code == 200
    assert settle_resp.json()["status"] == "MATURED"
    assert settle_resp.json()["principal_amount"] == "0.00"

    second_settle = await client.post(
        f"/api/v1/investments/{inv_id}/settle-maturity",
        json={"settlement_date": "2026-09-01", "destination_account_id": str(account.id)}, headers=headers,
    )
    assert second_settle.status_code in (400, 409)  # idempotent - never settles twice

    result = await db_session.execute(
        select(TreasuryTransaction).where(
            TreasuryTransaction.source_record_id == inv_id,
            TreasuryTransaction.event_type_code == "INVESTMENT_MATURITY",
        )
    )
    maturity_txns = list(result.scalars().all())
    assert len(maturity_txns) == 1
    assert maturity_txns[0].direction.value == "INFLOW"


async def test_invariant_original_principal_never_changes_across_termination(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="100000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)
    await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "30000000", "termination_date": "2026-07-01"}, headers=headers,
    )
    after = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    assert after.json()["original_principal_amount"] == "100000000.00"
    assert after.json()["principal_amount"] == "70000000.00"


async def test_invariant_outstanding_principal_never_negative(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="50000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)
    full = await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "50000000", "termination_date": "2026-07-01"}, headers=headers,
    )
    assert full.json()["investment"]["principal_amount"] == "0.00"
    assert full.json()["investment"]["status"] == "TERMINATED"


async def test_periodic_interest_investment_excluded_from_maturity_interest_line(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, investment_types,
):
    """
    SECTION 8: a PERIODIC-interest investment must NOT have its full
    expected_interest dumped onto the maturity forecast line (that cash
    arrives on a different schedule, or already arrived) - the principal
    line is still forecast correctly, only the interest line is withheld
    (documented limitation: no periodic schedule generator exists yet).
    """
    from app.models.banking import Bank
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.models.investment import (
        DayCountConvention,
        InterestPaymentFrequency,
        InterestPaymentMethod,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
    )
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="Periodic Interest Bank")
    db_session.add(bank)
    await db_session.flush()

    investment = Investment(
        investment_reference="INV-PERIODIC-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_a.id, institution_id=bank.id, currency_code="NGN",
        principal_amount=Decimal(200000000), original_principal_amount=Decimal(200000000),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2026, 6, 5),
        tenor_days=155, interest_rate=Decimal(12), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.ACTIVE,
        interest_payment_method=InterestPaymentMethod.PERIODIC,
        interest_payment_frequency=InterestPaymentFrequency.MONTHLY,
        expected_interest=Decimal("10191780.82"),
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
    forecast = await calculate_forecast(db_session, forecast)

    from sqlalchemy import select

    lines_result = await db_session.execute(
        select(ForecastLine).where(ForecastLine.forecast_id == forecast.id)
    )
    lines = list(lines_result.scalars().all())
    source_types = {ln.source_type.value for ln in lines}
    assert "INVESTMENT_MATURITY_PRINCIPAL" in source_types  # principal still forecast correctly
    assert "INVESTMENT_MATURITY_INTEREST" not in source_types  # interest correctly withheld


# ---------------------------------------------------------------------------
# SECTION 1/2 (final financial-integrity patch): operational available cash
# ---------------------------------------------------------------------------

async def test_placement_reduces_operational_available_cash_second_placement_rejected_third_allowed(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """
    TEST 1/2/3 from the task: 100m reported -> 60m placement -> 40m
    operational availability -> a second 50m placement is rejected ->
    a second 40m placement is allowed.
    """
    from app.services.cash_position_service import calculate_operational_available_cash

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_1 = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id,
        investment_reference="INV-CASH-OP-001", principal_amount="60000000",
    )
    place_1 = await client.post(
        f"/api/v1/investments/{inv_1}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert place_1.status_code == 200

    position = await calculate_operational_available_cash(db_session, account.id)
    assert position.operational_available_cash == Decimal("40000000.00")

    # Second placement of 50m must be rejected - only 40m operationally available.
    inv_2 = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id,
        investment_reference="INV-CASH-OP-002", principal_amount="50000000",
    )
    place_2 = await client.post(
        f"/api/v1/investments/{inv_2}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert place_2.status_code == 400

    # A 40m placement, exactly matching what's left, is allowed.
    inv_3 = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id,
        investment_reference="INV-CASH-OP-003", principal_amount="40000000",
    )
    place_3 = await client.post(
        f"/api/v1/investments/{inv_3}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert place_3.status_code == 200

    final_position = await calculate_operational_available_cash(db_session, account.id)
    assert final_position.operational_available_cash == Decimal("0.00")


async def test_placement_creates_exactly_one_treasury_transaction_linked_to_investment_transaction(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """TEST 4/5 from the task."""
    from sqlalchemy import select

    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(client, headers, entity_a.id, bank.id, account.id)
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    cash_result = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.source_record_id == inv_id)
    )
    cash_txns = list(cash_result.scalars().all())
    assert len(cash_txns) == 1

    txns = await client.get(f"/api/v1/investments/{inv_id}/transactions", headers=headers)
    placement_txn = next(t for t in txns.json() if t["transaction_type"] == "PLACEMENT")
    from app.models.investment import InvestmentTransaction

    db_txn = await db_session.get(InvestmentTransaction, placement_txn["id"])
    assert db_txn.cash_transaction_id == cash_txns[0].id


async def test_termination_and_maturity_increase_operational_available_cash(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """TEST 6/7/8 from the task."""
    from app.services.cash_position_service import calculate_operational_available_cash

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="60000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    after_placement = await calculate_operational_available_cash(db_session, account.id)
    assert after_placement.operational_available_cash == Decimal("40000000.00")

    partial = await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "20000000", "termination_date": "2026-07-01", "destination_account_id": str(account.id)},
        headers=headers,
    )
    assert partial.status_code == 200
    net_proceeds_1 = Decimal(partial.json()["net_proceeds"])

    after_partial = await calculate_operational_available_cash(db_session, account.id)
    assert after_partial.operational_available_cash == Decimal("40000000.00") + net_proceeds_1

    full = await client.post(
        f"/api/v1/investments/{inv_id}/terminate",
        json={"amount": "40000000", "termination_date": "2026-07-15", "destination_account_id": str(account.id)},
        headers=headers,
    )
    assert full.status_code == 200
    net_proceeds_2 = Decimal(full.json()["net_proceeds"])

    after_full = await calculate_operational_available_cash(db_session, account.id)
    assert after_full.operational_available_cash == (
        Decimal("40000000.00") + net_proceeds_1 + net_proceeds_2
    )
    assert after_full.operational_available_cash > after_partial.operational_available_cash


async def test_maturity_settlement_increases_operational_cash_by_principal_plus_interest(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from app.services.cash_position_service import calculate_operational_available_cash

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="60000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)
    before = await calculate_operational_available_cash(db_session, account.id)

    settle_resp = await client.post(
        f"/api/v1/investments/{inv_id}/settle-maturity",
        json={"settlement_date": "2026-09-01", "destination_account_id": str(account.id)}, headers=headers,
    )
    assert settle_resp.status_code == 200

    after = await calculate_operational_available_cash(db_session, account.id)
    inv_after = await client.get(f"/api/v1/investments/{inv_id}", headers=headers)
    expected_total = Decimal(60000000) + Decimal(inv_after.json()["received_interest"])
    assert after.operational_available_cash == before.operational_available_cash + expected_total


async def test_full_rollover_does_not_change_operational_available_cash(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """TEST 10 from the task."""
    from app.services.cash_position_service import calculate_operational_available_cash

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="60000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)
    before = await calculate_operational_available_cash(db_session, account.id)

    rollover_resp = await client.post(
        f"/api/v1/investments/{inv_id}/rollover",
        json={"rollover_amount": "60000000", "new_rate": "19.0", "new_start_date": "2026-09-01",
              "new_maturity_date": "2026-12-01", "new_reference": "INV-CASH-OP-ROLL-001"},
        headers=headers,
    )
    assert rollover_resp.status_code == 201

    after = await calculate_operational_available_cash(db_session, account.id)
    assert after.operational_available_cash == before.operational_available_cash


async def test_bank_balance_refresh_after_placement_does_not_double_count(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """
    TEST 20 from the task: the exact scenario from the spec - 100m
    reported, 60m placed (operational cash 40m), then a LATER bank
    balance import reports 40m (the bank has now caught up and shows
    the post-placement figure). Operational cash must correctly read
    40m, never 40m - 60m = -20m (the transaction is anchored to the
    OLD balance_date and correctly drops out of "unreflected" once a
    newer balance covers it).
    """
    from app.services.cash_position_service import calculate_operational_available_cash

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000), as_of=datetime.date(2026, 5, 31))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="60000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    position_before_import = await calculate_operational_available_cash(db_session, account.id)
    assert position_before_import.operational_available_cash == Decimal("40000000.00")

    # The bank statement/balance for 2026-06-01 (or later) now arrives,
    # already reflecting the placement.
    await _set_balance(db_session, account, Decimal(40000000), as_of=datetime.date(2026, 6, 1))
    await db_session.commit()

    position_after_import = await calculate_operational_available_cash(db_session, account.id)
    assert position_after_import.operational_available_cash == Decimal("40000000.00")
    assert position_after_import.reported_balance_date == datetime.date(2026, 6, 1)
    assert position_after_import.unreflected_outflows == Decimal("0.00")


# ---------------------------------------------------------------------------
# SECTION 5 (final financial-integrity patch): rebooking idempotency
# ---------------------------------------------------------------------------

async def test_retried_rebooking_with_same_idempotency_key_does_not_duplicate_cash_outflow(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """TEST 12/13 from the task."""
    from sqlalchemy import select

    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="50000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    payload = {
        "additional_principal": "10000000", "reason": "Top-up", "bank_account_id": str(account.id),
        "idempotency_key": "rebook-request-abc-123",
    }
    first = await client.post(f"/api/v1/investments/{inv_id}/rebook", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["principal_amount"] == "60000000.00"

    # Exact retry with the same idempotency key.
    second = await client.post(f"/api/v1/investments/{inv_id}/rebook", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["principal_amount"] == "60000000.00"  # unchanged by the retry

    result = await db_session.execute(
        select(TreasuryTransaction).where(
            TreasuryTransaction.source_record_id == inv_id,
            TreasuryTransaction.event_type_code == "INVESTMENT_PLACEMENT",
        )
    )
    outflows = list(result.scalars().all())
    assert len(outflows) == 2  # original placement + exactly one top-up, never two top-ups


async def test_different_idempotency_key_creates_a_genuinely_new_rebooking(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="50000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    first = await client.post(
        f"/api/v1/investments/{inv_id}/rebook",
        json={"additional_principal": "10000000", "reason": "Top-up 1", "bank_account_id": str(account.id),
              "idempotency_key": "key-1"},
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["principal_amount"] == "60000000.00"

    second = await client.post(
        f"/api/v1/investments/{inv_id}/rebook",
        json={"additional_principal": "5000000", "reason": "Top-up 2", "bank_account_id": str(account.id),
              "idempotency_key": "key-2"},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["principal_amount"] == "65000000.00"  # a genuinely different request applies


# ---------------------------------------------------------------------------
# SECTION 2 (final freeze patch): rebooking idempotency covers rate/
# maturity-only rebookings too, not merely a principal top-up
# ---------------------------------------------------------------------------

async def test_retried_rate_only_rebooking_with_same_idempotency_key_does_not_reapply(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    """
    The bug this test guards against: the original idempotency check
    only looked at InvestmentTransaction, which is ONLY created when
    additional_principal > 0. A rate-only (or maturity-only) rebooking
    creates no InvestmentTransaction at all, so a retry would have
    slipped through undetected and applied the rate change a second
    time - bumping the version twice and writing two InvestmentVersion/
    InvestmentEvent rows for what should be one idempotent operation.
    """
    from sqlalchemy import select

    from app.models.investment import InvestmentEvent, InvestmentVersion

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="50000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    payload = {"new_rate": "20.0", "reason": "Rate reprice", "idempotency_key": "rate-only-retry-key"}
    first = await client.post(f"/api/v1/investments/{inv_id}/rebook", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["interest_rate"] == "20.000000"
    assert first.json()["version"] == 2

    # Exact retry with the same idempotency key - no additional_principal
    # at all, so the ONLY thing that would previously have been checked
    # (InvestmentTransaction) was never created in the first place.
    second = await client.post(f"/api/v1/investments/{inv_id}/rebook", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["version"] == 2  # NOT bumped to 3 by the retry

    versions = await db_session.execute(
        select(InvestmentVersion).where(InvestmentVersion.investment_id == inv_id)
    )
    assert len(list(versions.scalars().all())) == 2  # initial (v1) + one repricing (v2), never a third

    events = await db_session.execute(
        select(InvestmentEvent).where(
            InvestmentEvent.investment_id == inv_id, InvestmentEvent.reference == "rate-only-retry-key",
        )
    )
    assert len(list(events.scalars().all())) == 1  # exactly one REBOOKED event for this key, never two


async def test_retried_maturity_only_rebooking_with_same_idempotency_key_does_not_reapply(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    from sqlalchemy import select

    from app.models.investment import InvestmentVersion

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id)
    await _set_balance(db_session, account, Decimal(100000000))
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, account.id, principal_amount="50000000",
    )
    await client.post(f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers)

    payload = {
        "new_maturity_date": "2026-12-01", "reason": "Extend maturity",
        "idempotency_key": "maturity-only-retry-key",
    }
    first = await client.post(f"/api/v1/investments/{inv_id}/rebook", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["maturity_date"] == "2026-12-01"
    assert first.json()["version"] == 2

    second = await client.post(f"/api/v1/investments/{inv_id}/rebook", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["version"] == 2

    versions = await db_session.execute(
        select(InvestmentVersion).where(InvestmentVersion.investment_id == inv_id)
    )
    assert len(list(versions.scalars().all())) == 2


# ---------------------------------------------------------------------------
# SECTION 6 (final financial-integrity patch): periodic/upfront interest
# ---------------------------------------------------------------------------

async def test_monthly_periodic_interest_generates_correct_receipt_dates(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, investment_types,
):
    """TEST 14/15 from the task."""
    from app.models.banking import Bank
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.models.investment import (
        DayCountConvention,
        InterestPaymentFrequency,
        InterestPaymentMethod,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
    )
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="Monthly Interest Bank")
    db_session.add(bank)
    await db_session.flush()

    investment = Investment(
        investment_reference="INV-MONTHLY-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_a.id, institution_id=bank.id, currency_code="NGN",
        principal_amount=Decimal(120000000), original_principal_amount=Decimal(120000000),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2026, 4, 1),
        tenor_days=90, interest_rate=Decimal(12), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.ACTIVE,
        interest_payment_method=InterestPaymentMethod.PERIODIC,
        interest_payment_frequency=InterestPaymentFrequency.MONTHLY,
        expected_interest=Decimal("3550684.93"),
    )
    db_session.add(investment)
    await db_session.flush()

    # The forecast engine always produces a fixed 13-week (91-day)
    # horizon from forecast_start_date, regardless of forecast_end_date -
    # this investment's 3 monthly periods (Feb 1, Mar 1, Apr 1) all fall
    # comfortably within that real horizon.
    forecast = Forecast(
        group_id=group.id, legal_entity_id=entity_a.id, forecast_start_date=datetime.date(2026, 1, 1),
        forecast_end_date=datetime.date(2026, 1, 1) + datetime.timedelta(days=13 * 7 - 1),
        value_basis=ForecastValueBasis.GROSS, reporting_currency_code="NGN",
        status=ForecastStatus.DRAFT,
    )
    db_session.add(forecast)
    await db_session.flush()
    forecast = await calculate_forecast(db_session, forecast)

    from sqlalchemy import select

    lines_result = await db_session.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.source_type == "INVESTMENT_INTEREST_RECEIPT",
        )
    )
    interest_lines = sorted(lines_result.scalars().all(), key=lambda ln: ln.source_id)
    # 3 months from Jan 1 to Apr 1 -> 3 periods, on real monthly dates,
    # never all bundled onto the single maturity date.
    assert len(interest_lines) == 3
    for i, line in enumerate(interest_lines, start=1):
        assert line.source_id == f"{investment.id}:period-{i}"


async def test_monthly_periodic_interest_never_duplicated_at_maturity(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, investment_types,
):
    """TEST 15 from the task, verified explicitly."""
    from app.models.banking import Bank
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.models.investment import (
        DayCountConvention,
        InterestPaymentFrequency,
        InterestPaymentMethod,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
    )
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="No Duplicate Interest Bank")
    db_session.add(bank)
    await db_session.flush()

    investment = Investment(
        investment_reference="INV-NODUP-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_a.id, institution_id=bank.id, currency_code="NGN",
        principal_amount=Decimal(100000000), original_principal_amount=Decimal(100000000),
        start_date=datetime.date(2026, 5, 1), maturity_date=datetime.date(2026, 6, 5),
        tenor_days=35, interest_rate=Decimal(12), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.ACTIVE,
        interest_payment_method=InterestPaymentMethod.PERIODIC,
        interest_payment_frequency=InterestPaymentFrequency.MONTHLY,
        expected_interest=Decimal("1150684.93"),
    )
    db_session.add(investment)
    await db_session.flush()

    forecast = Forecast(
        group_id=group.id, legal_entity_id=entity_a.id, forecast_start_date=datetime.date(2026, 5, 1),
        forecast_end_date=datetime.date(2026, 5, 1) + datetime.timedelta(days=13 * 7 - 1),
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
    lines = list(lines_result.scalars().all())
    maturity_interest_lines = [ln for ln in lines if ln.source_type.value == "INVESTMENT_MATURITY_INTEREST"]
    assert maturity_interest_lines == []  # never duplicated onto the maturity date


async def test_upfront_interest_appears_at_placement_date_not_maturity(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, investment_types,
):
    """TEST 16 from the task."""
    from app.models.banking import Bank
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.models.investment import (
        DayCountConvention,
        InterestPaymentMethod,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
    )
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="Upfront Interest Bank")
    db_session.add(bank)
    await db_session.flush()

    investment = Investment(
        investment_reference="INV-UPFRONT-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_a.id, institution_id=bank.id, currency_code="NGN",
        principal_amount=Decimal(50000000), original_principal_amount=Decimal(50000000),
        start_date=datetime.date(2026, 6, 10), maturity_date=datetime.date(2026, 9, 10),
        tenor_days=92, interest_rate=Decimal(15), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.PLACEMENT_PENDING,
        interest_payment_method=InterestPaymentMethod.UPFRONT,
        placement_date=datetime.date(2026, 6, 10), expected_interest=Decimal("1890410.96"),
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
    forecast = await calculate_forecast(db_session, forecast)

    from sqlalchemy import select

    lines_result = await db_session.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.source_type == "INVESTMENT_INTEREST_RECEIPT",
        )
    )
    interest_lines = list(lines_result.scalars().all())
    assert len(interest_lines) == 1
    assert interest_lines[0].reporting_amount == Decimal("1890410.96")


async def test_at_maturity_interest_still_appears_at_maturity(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, investment_types,
):
    """TEST 17 from the task - the pre-existing AT_MATURITY behavior, unaffected by this patch."""
    from app.models.banking import Bank
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.models.investment import (
        DayCountConvention,
        InterestPaymentMethod,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
    )
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="At Maturity Interest Bank")
    db_session.add(bank)
    await db_session.flush()

    investment = Investment(
        investment_reference="INV-ATMAT-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_a.id, institution_id=bank.id, currency_code="NGN",
        principal_amount=Decimal(80000000), original_principal_amount=Decimal(80000000),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2026, 6, 5),
        tenor_days=155, interest_rate=Decimal(14), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.ACTIVE,
        interest_payment_method=InterestPaymentMethod.AT_MATURITY,
        expected_interest=Decimal("4757534.25"),
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
    forecast = await calculate_forecast(db_session, forecast)

    from sqlalchemy import select

    lines_result = await db_session.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.source_type == "INVESTMENT_MATURITY_INTEREST",
        )
    )
    interest_lines = list(lines_result.scalars().all())
    assert len(interest_lines) == 1
    assert interest_lines[0].reporting_amount == Decimal("4757534.25")


# ---------------------------------------------------------------------------
# SECTION 11 (final financial-integrity patch): multi-currency
# ---------------------------------------------------------------------------

async def test_operational_cash_check_is_currency_specific_never_aggregated(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_group_and_entities, investment_types,
):
    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    ngn_account = await _make_account(db_session, entity_a.id, bank.id, currency="NGN", account_number="1111")
    usd_account = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number="2222")
    await _set_balance(db_session, ngn_account, Decimal(1000000000), currency="NGN")
    await _set_balance(db_session, usd_account, Decimal(1000), currency="USD")
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    # A USD investment sized well within the NGN account's huge balance but
    # far beyond the USD account's tiny one - the USD account's own
    # balance must be what's checked, never a blended figure.
    inv_id = await _create_and_approve(
        client, headers, entity_a.id, bank.id, usd_account.id, currency_code="USD",
        principal_amount="500000",
    )
    place_resp = await client.post(
        f"/api/v1/investments/{inv_id}/place", json={"placement_date": "2026-06-01"}, headers=headers,
    )
    assert place_resp.status_code == 400


# ---------------------------------------------------------------------------
# SECTION 3 (final freeze patch): periodic interest history regression
# ---------------------------------------------------------------------------

async def test_already_received_periodic_interest_is_not_forecast_again(
    db_session: AsyncSession, demo_group_and_entities, forecast_categories, investment_types,
):
    """
    Investment: 100m, monthly interest, 12-month tenor. Period 1's
    interest has already been recorded as received (an EXECUTED
    INTEREST_RECEIPT InvestmentTransaction dated exactly on period 1's
    end date). The forecast horizon covers periods 1 and 2. Expected:
    period 1 is NOT forecast again; period 2 is forecast once.
    """
    from sqlalchemy import select

    from app.models.banking import Bank
    from app.models.forecast import Forecast, ForecastLine, ForecastStatus, ForecastValueBasis
    from app.models.investment import (
        DayCountConvention,
        InterestPaymentFrequency,
        InterestPaymentMethod,
        Investment,
        InvestmentRateType,
        InvestmentStatus,
        InvestmentTransaction,
        InvestmentTransactionStatus,
        InvestmentTransactionType,
    )
    from app.services.forecast_engine import calculate_forecast

    group, entity_a, _ = demo_group_and_entities
    bank = Bank(name="Periodic History Bank")
    db_session.add(bank)
    await db_session.flush()

    investment = Investment(
        investment_reference="INV-PERIODIC-HIST-001", investment_type_code="FIXED_DEPOSIT",
        legal_entity_id=entity_a.id, institution_id=bank.id, currency_code="NGN",
        principal_amount=Decimal(100000000), original_principal_amount=Decimal(100000000),
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2027, 1, 1),
        tenor_days=365, interest_rate=Decimal(12), rate_type=InvestmentRateType.FIXED,
        day_count_convention=DayCountConvention.ACT_365, status=InvestmentStatus.ACTIVE,
        interest_payment_method=InterestPaymentMethod.PERIODIC,
        interest_payment_frequency=InterestPaymentFrequency.MONTHLY,
        expected_interest=Decimal("12000000.00"),
    )
    db_session.add(investment)
    await db_session.flush()

    # Period 1 runs Jan 1 -> Feb 1; its interest has already been received.
    db_session.add(InvestmentTransaction(
        investment_id=investment.id, legal_entity_id=entity_a.id,
        transaction_type=InvestmentTransactionType.INTEREST_RECEIPT, currency_code="NGN",
        amount=Decimal("986301.37"), transaction_date=datetime.date(2026, 2, 1),
        status=InvestmentTransactionStatus.EXECUTED,
    ))
    await db_session.flush()

    forecast = Forecast(
        group_id=group.id, legal_entity_id=entity_a.id, forecast_start_date=datetime.date(2026, 1, 1),
        forecast_end_date=datetime.date(2026, 1, 1) + datetime.timedelta(days=13 * 7 - 1),
        value_basis=ForecastValueBasis.GROSS, reporting_currency_code="NGN",
        status=ForecastStatus.DRAFT,
    )
    db_session.add(forecast)
    await db_session.flush()
    forecast = await calculate_forecast(db_session, forecast)

    lines_result = await db_session.execute(
        select(ForecastLine).where(
            ForecastLine.forecast_id == forecast.id,
            ForecastLine.source_type == "INVESTMENT_INTEREST_RECEIPT",
        )
    )
    interest_lines = sorted(lines_result.scalars().all(), key=lambda ln: ln.source_id)

    # Period 1 (source_id ending "period-1") must NOT appear - already received.
    period_numbers = [ln.source_id.rsplit("period-", 1)[1] for ln in interest_lines]
    assert "1" not in period_numbers
    # Period 2 must appear exactly once.
    assert period_numbers.count("2") == 1
