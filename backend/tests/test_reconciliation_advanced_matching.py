import asyncio
import datetime
import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_data_model import _login, _make_scoped_user
from tests.test_reconciliation_matching_engine import (
    _create_ready_run,
    _make_bank_statement_txn,
    _make_treasury_txn,
    _set_configuration,
)


async def _get_match_groups(client, headers, run_id):
    resp = await client.get(f"/api/v1/reconciliation/runs/{run_id}/match-groups", headers=headers)
    assert resp.status_code == 200
    return resp.json()


async def _make_fx_rate(db_session, from_currency, to_currency, rate, rate_date, rate_type=None, rate_source="TEST", version=1, is_current=True):
    from app.models.currency import FXRate, FXRateType

    fx_rate = FXRate(
        from_currency_code=from_currency, to_currency_code=to_currency,
        rate_type=rate_type or FXRateType.SPOT, rate_date=rate_date, rate=Decimal(rate),
        rate_source=rate_source, version=version, is_current=is_current,
    )
    db_session.add(fx_rate)
    await db_session.flush()
    return fx_rate


async def test_one_to_many_group_created_when_aggregate_matches(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="6666000011")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="onetomany",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="10000000",
        bank_reference="PAYGROUP-001",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="INFLOW",
        reference="PAYGROUP-001",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="3000000", direction="INFLOW",
        reference="PAYGROUP-001",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="3000000", direction="INFLOW",
        reference="PAYGROUP-001",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.status_code == 200
    body = execute_resp.json()
    assert body["advanced_match_count"] == 1
    assert body["ambiguous_advanced_count"] == 0

    groups = await _get_match_groups(client, headers, run_id)
    assert len(groups) == 1
    assert groups[0]["relationship_type"] == "ONE_TO_MANY"
    assert groups[0]["status"] == "PENDING"
    assert len(groups[0]["members"]) == 4
    assert groups[0]["bank_aggregate_amount"] == "10000000.00"
    assert groups[0]["ledger_aggregate_amount"] == "10000000.00"


async def test_many_to_one_group_created_when_aggregate_matches(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="6666000022")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="manytoone",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000",
        bank_reference="SETTLE-001",
    )
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="3000000",
        bank_reference="SETTLE-001", row_number=3,
    )
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="3000000",
        bank_reference="SETTLE-001", row_number=4,
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="10000000", direction="INFLOW",
        reference="SETTLE-001",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["advanced_match_count"] == 1

    groups = await _get_match_groups(client, headers, run_id)
    assert len(groups) == 1
    assert groups[0]["relationship_type"] == "MANY_TO_ONE"
    assert len(groups[0]["members"]) == 4


async def test_batch_relationship_type_when_batch_keyword_present(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="6666000033")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="batchkeyword",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="7000000",
        bank_reference="PAYROLL-BATCH-2026-06", narration="Payroll batch settlement",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="INFLOW",
        reference="PAYROLL-BATCH-2026-06",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="3000000", direction="INFLOW",
        reference="PAYROLL-BATCH-2026-06",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)

    groups = await _get_match_groups(client, headers, run_id)
    assert len(groups) == 1
    assert groups[0]["relationship_type"] == "BATCH"


async def test_amount_only_coincidence_does_not_create_batch_match(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="6666000044")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="amountonlyno",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="7000000", bank_reference="NO-MATCH-BANK",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="INFLOW",
        reference="UNRELATED-A",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="3000000", direction="INFLOW",
        reference="UNRELATED-B",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0

    groups = await _get_match_groups(client, headers, run_id)
    assert len(groups) == 0


async def test_internal_transfer_recognized_via_transfer_pair_id(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="6666000055")
    account_b = await _make_account(db_session, entity_a.id, bank.id, account_number="6666000066")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="transferrecognized",
    )
    await db_session.flush()

    pair_id = uuid.uuid4()
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, entry_type="DEBIT",
        bank_reference="XFER-001",
    )
    outflow_leg = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, direction="OUTFLOW", reference="XFER-001",
    )
    outflow_leg.transfer_pair_id = pair_id
    inflow_leg = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account_b.id, direction="INFLOW", reference="XFER-001",
    )
    inflow_leg.transfer_pair_id = pair_id
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account_a.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["matched_count"] == 1
    assert execute_resp.json()["advanced_match_count"] == 1

    groups = await _get_match_groups(client, headers, run_id)
    transfer_groups = [g for g in groups if g["relationship_type"] == "INTERNAL_TRANSFER"]
    assert len(transfer_groups) == 1
    member_treasury_ids = {m["treasury_transaction_id"] for m in transfer_groups[0]["members"]}
    assert member_treasury_ids == {str(outflow_leg.id), str(inflow_leg.id)}


async def test_unrelated_same_amount_transactions_not_classified_as_transfer(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="6666000077")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="notransferinfer",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, bank_reference="NOTXFER-001",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, direction="INFLOW", reference="NOTXFER-001",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account_a.id)
    await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)

    groups = await _get_match_groups(client, headers, run_id)
    assert not any(g["relationship_type"] == "INTERNAL_TRANSFER" for g in groups)


async def test_fx_match_created_when_authoritative_rate_exists_and_within_tolerance(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number="6666000088")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fxvalid",
    )
    await db_session.flush()

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 6, 10))
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="USD", amount="10000",
        bank_reference="FX-001",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="NGN", amount="16000000",
        direction="INFLOW", reference="FX-001",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id)

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 1

    groups = await _get_match_groups(client, headers, run_id)
    fx_groups = [g for g in groups if g["relationship_type"] == "FX_MATCH"]
    assert len(fx_groups) == 1
    assert fx_groups[0]["fx_rate"] == "1600.0000000000"
    assert fx_groups[0]["fx_source_currency_code"] == "USD"
    assert fx_groups[0]["fx_target_currency_code"] == "NGN"


async def test_fx_no_match_when_converted_amount_outside_tolerance(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number="6666000099")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fxoutsidetol",
    )
    await db_session.flush()

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 6, 10))
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="USD", amount="10000",
        bank_reference="FX-BADTOL",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="NGN", amount="12000000",
        direction="INFLOW", reference="FX-BADTOL",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0

    groups = await _get_match_groups(client, headers, run_id)
    assert not any(g["relationship_type"] == "FX_MATCH" for g in groups)


async def test_fx_no_match_when_no_authoritative_rate_exists(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number="6666000100")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fxnorate",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="USD", amount="10000",
        bank_reference="FX-NORATE",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="NGN", amount="16000000",
        direction="INFLOW", reference="FX-NORATE",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_fx_no_match_when_rate_dated_after_transaction(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number="6666000111")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fxfuturerate",
    )
    await db_session.flush()

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 7, 1))
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="USD", amount="10000",
        txn_date="2026-06-15", bank_reference="FX-FUTURERATE",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="NGN", amount="16000000",
        event_date="2026-06-15", direction="INFLOW", reference="FX-FUTURERATE",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_cross_entity_fx_candidate_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number="6666000122")
    account_b = await _make_account(db_session, entity_b.id, bank.id, currency="NGN", account_number="6666000133")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fxcrossentity",
    )
    await db_session.flush()

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 6, 10))
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, currency="USD", amount="10000",
        bank_reference="FX-CROSSENT",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_b.id, account_id=account_b.id, currency="NGN", amount="16000000",
        direction="INFLOW", reference="FX-CROSSENT",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account_a.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_ambiguous_grouping_persists_all_competing_subsets_never_arbitrary(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    """
    Bank = 10,000,000. Ledger candidates sharing the same reference:
    9,500,000 + 500,000 + 500,000 = 10,500,000 (full set outside
    tolerance). Dropping EITHER 500,000 row gives exactly 10,000,000 -
    two equally valid groupings, genuinely ambiguous (which 500,000 row
    was the "extra" one is undecidable).
    """
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000011")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="ambiguousgroup",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="10000000",
        bank_reference="PAYGROUP-AMBIG",
    )
    for amount in ("9500000", "500000", "500000"):
        await _make_treasury_txn(
            db_session, entity_id=entity_a.id, account_id=account.id, amount=amount, direction="INFLOW",
            reference="PAYGROUP-AMBIG",
        )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["ambiguous_advanced_count"] == 1
    assert body["advanced_match_count"] == 0

    groups = await _get_match_groups(client, headers, run_id)
    ambiguous_groups = [g for g in groups if g["status"] == "AMBIGUOUS"]
    assert len(ambiguous_groups) == 2
    for g in ambiguous_groups:
        assert len(g["members"]) == 3
        assert "ambiguous" in g["reason"].lower()


async def test_claimed_treasury_transaction_not_reused_in_unrelated_advanced_group(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000022")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="claimedprotect",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="5000000", bank_reference="ONE-TO-ONE",
    )
    already_matched_ledger = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="5000000", direction="INFLOW",
        reference="ONE-TO-ONE",
    )
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="8000000",
        bank_reference="ONE-TO-ONE", row_number=3,
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="3000000", direction="INFLOW",
        reference="ONE-TO-ONE",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["matched_count"] == 1
    assert body["advanced_match_count"] == 0

    groups = await _get_match_groups(client, headers, run_id)
    for g in groups:
        member_treasury_ids = {m["treasury_transaction_id"] for m in g["members"] if m["treasury_transaction_id"]}
        assert str(already_matched_ledger.id) not in member_treasury_ids


async def test_grouping_never_crosses_entity_boundary(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000033")
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="7777000044")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="groupentityiso",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, amount="10000000",
        bank_reference="PAYGROUP-CROSSENT",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, amount="4000000", direction="INFLOW",
        reference="PAYGROUP-CROSSENT",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_b.id, account_id=account_b.id, amount="6000000", direction="INFLOW",
        reference="PAYGROUP-CROSSENT",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account_a.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_non_fx_grouping_never_crosses_currency(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000055")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="groupcurrencyiso",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="NGN", amount="10000000",
        bank_reference="PAYGROUP-CURRISO",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="NGN", amount="4000000",
        direction="INFLOW", reference="PAYGROUP-CURRISO",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="USD", amount="6000000",
        direction="INFLOW", reference="PAYGROUP-CURRISO",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_direction_mismatch_cannot_form_advanced_group(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000066")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="groupdirection",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, entry_type="CREDIT", amount="10000000",
        bank_reference="PAYGROUP-BADDIR",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="OUTFLOW",
        reference="PAYGROUP-BADDIR",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="6000000", direction="OUTFLOW",
        reference="PAYGROUP-BADDIR",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_transaction_outside_configured_date_span_cannot_join_group(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000077")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="groupdatespan",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="10000000", txn_date="2026-06-15",
        bank_reference="PAYGROUP-DATESPAN",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="INFLOW",
        event_date="2026-06-15", reference="PAYGROUP-DATESPAN",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="6000000", direction="INFLOW",
        event_date="2026-01-01", reference="PAYGROUP-DATESPAN",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, date_tolerance_days=2)

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_grouped_aggregate_exact_match(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000088")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="groupexact",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="5000000",
        bank_reference="PAYGROUP-EXACT",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="2500000", direction="INFLOW",
        reference="PAYGROUP-EXACT",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="2500000", direction="INFLOW",
        reference="PAYGROUP-EXACT",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 1

    groups = await _get_match_groups(client, headers, run_id)
    assert groups[0]["difference"] == "0.00"


async def test_grouped_aggregate_within_tolerance(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000099")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="grouptol",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="5000000",
        bank_reference="PAYGROUP-TOL",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="2500000", direction="INFLOW",
        reference="PAYGROUP-TOL",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="2497500", direction="INFLOW",
        reference="PAYGROUP-TOL",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, amount_tolerance_pct="0.1")

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 1


async def test_grouped_aggregate_outside_tolerance_no_match(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000200")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="groupnottol",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="5000000",
        bank_reference="PAYGROUP-NOTOL",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="2500000", direction="INFLOW",
        reference="PAYGROUP-NOTOL",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="2000000", direction="INFLOW",
        reference="PAYGROUP-NOTOL",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, amount_tolerance_pct="0.1")

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_reversed_bank_transaction_never_enters_advanced_matching(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.bank_statement import BankStatementTransactionStatus
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000211")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="groupreversed",
    )
    await db_session.flush()

    reversed_txn = await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="10000000",
        bank_reference="PAYGROUP-REVERSED",
    )
    reversed_txn.status = BankStatementTransactionStatus.REVERSED
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="INFLOW",
        reference="PAYGROUP-REVERSED",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="6000000", direction="INFLOW",
        reference="PAYGROUP-REVERSED",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 0


async def test_repeated_advanced_matching_pass_does_not_duplicate_groups(
    db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from sqlalchemy import select

    from app.models.reconciliation import (
        ReconciliationMatchGroup,
        ReconciliationRun,
        ReconciliationRunStatus,
    )
    from app.services.reconciliation_advanced_matching import run_advanced_matching_for_run
    from app.services.reconciliation_matching_engine import run_matching_for_run

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000222")

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="10000000",
        bank_reference="PAYGROUP-IDEMP",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="INFLOW",
        reference="PAYGROUP-IDEMP",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="6000000", direction="INFLOW",
        reference="PAYGROUP-IDEMP",
    )
    run = ReconciliationRun(
        legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=datetime.date(2026, 6, 1), period_end=datetime.date(2026, 6, 30),
        status=ReconciliationRunStatus.RUNNING,
    )
    db_session.add(run)
    await db_session.flush()

    await run_matching_for_run(
        db_session, run_id=run.id, legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=run.period_start, period_end=run.period_end, configuration=None,
    )
    await run_advanced_matching_for_run(
        db_session, run_id=run.id, legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=run.period_start, period_end=run.period_end, configuration=None,
    )
    await run_advanced_matching_for_run(
        db_session, run_id=run.id, legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=run.period_start, period_end=run.period_end, configuration=None,
    )

    result = await db_session.execute(
        select(ReconciliationMatchGroup).where(ReconciliationMatchGroup.reconciliation_run_id == run.id)
    )
    assert len(list(result.scalars().all())) == 1


async def test_advanced_matching_never_modifies_treasury_transaction_or_bank_balance(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from sqlalchemy import select

    from app.models.balance import BankBalance
    from app.models.rbac import EntityScopeType
    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000233")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="groupnofinance",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="10000000",
        bank_reference="PAYGROUP-NOFX",
    )
    ledger_1 = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="INFLOW",
        reference="PAYGROUP-NOFX",
    )
    ledger_2 = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="6000000", direction="INFLOW",
        reference="PAYGROUP-NOFX",
    )
    balance = BankBalance(
        bank_account_id=account.id, balance_date=datetime.date(2026, 5, 31), currency_code="NGN",
        closing_balance=Decimal(5000000), available_balance=Decimal(5000000), source="MANUAL",
    )
    db_session.add(balance)
    await db_session.commit()

    original_amounts = {ledger_1.id: ledger_1.transaction_amount, ledger_2.id: ledger_2.transaction_amount}
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["advanced_match_count"] == 1

    for txn_id, original_amount in original_amounts.items():
        refreshed = await db_session.get(TreasuryTransaction, txn_id)
        assert refreshed.transaction_amount == original_amount

    balance_after = await db_session.get(BankBalance, balance.id)
    assert balance_after.closing_balance == Decimal("5000000.00")

    all_treasury = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.bank_account_id == account.id)
    )
    assert len(list(all_treasury.scalars().all())) == 2


async def test_advanced_matching_candidate_pool_remains_bounded_at_scale(
    db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.services.reconciliation_advanced_matching import _reference_matched_ledger_candidates

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7777000244")

    bank_txn = await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="10000000",
        bank_reference="PAYGROUP-SCALE",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="4000000", direction="INFLOW",
        reference="PAYGROUP-SCALE",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="6000000", direction="INFLOW",
        reference="PAYGROUP-SCALE",
    )
    for i in range(200):
        await _make_treasury_txn(
            db_session, entity_id=entity_a.id, account_id=account.id, amount="1000000", direction="INFLOW",
            reference=f"UNRELATED-SCALE-{i}",
        )
    await db_session.commit()

    candidates = await _reference_matched_ledger_candidates(
        db_session, bank_txn, date_tolerance_days=0, excluded_treasury_transaction_ids=set(), max_candidates=20,
    )
    assert len(candidates) == 2


# ---------------------------------------------------------------------------
# Stage 5D concurrency (exercising the advanced pass specifically)
# ---------------------------------------------------------------------------

async def _seed_group_scenario(db_session, entity_id, account_id, reference):
    await _make_bank_statement_txn(
        db_session, entity_id=entity_id, account_id=account_id, amount="10000000", bank_reference=reference,
    )
    for amount in ("4000000", "6000000"):
        await _make_treasury_txn(
            db_session, entity_id=entity_id, account_id=account_id, amount=amount, direction="INFLOW",
            reference=reference,
        )


async def test_same_run_concurrent_execution_creates_exactly_one_group(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from sqlalchemy import select

    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationMatchGroup

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="9999000011")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="adv5dsamerun",
    )
    await db_session.flush()
    await _seed_group_scenario(db_session, entity_a.id, account.id, "PAYGROUP-CONC1")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    results = await asyncio.gather(
        *[client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers) for _ in range(2)],
        return_exceptions=True,
    )
    codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert codes.count(200) == 1 and 400 in codes

    groups = (await db_session.execute(
        select(ReconciliationMatchGroup).where(ReconciliationMatchGroup.reconciliation_run_id == uuid.UUID(run_id))
    )).scalars().all()
    assert len(groups) == 1


async def test_different_runs_same_scope_never_claim_same_ledger_rows_in_groups(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from sqlalchemy import select

    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationMatchGroupMember

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="9999000022")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="adv5dcrossrun",
    )
    await db_session.flush()
    await _seed_group_scenario(db_session, entity_a.id, account.id, "PAYGROUP-CONC2")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_a = await _create_ready_run(client, headers, entity_a.id, account.id)
    run_b = await _create_ready_run(client, headers, entity_a.id, account.id)
    results = await asyncio.gather(
        client.post(f"/api/v1/reconciliation/runs/{run_a}/execute", headers=headers),
        client.post(f"/api/v1/reconciliation/runs/{run_b}/execute", headers=headers),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results)

    members = (await db_session.execute(
        select(ReconciliationMatchGroupMember.treasury_transaction_id).where(
            ReconciliationMatchGroupMember.treasury_transaction_id.is_not(None)
        )
    )).scalars().all()
    # Each ledger row appears in at most one group across BOTH runs.
    assert len(members) == len(set(members))


async def test_different_scopes_execute_advanced_matching_concurrently(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    acct1 = await _make_account(db_session, entity_a.id, bank.id, account_number="9999000033")
    acct2 = await _make_account(db_session, entity_a.id, bank.id, account_number="9999000044")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="adv5dindep",
    )
    await db_session.flush()
    await _seed_group_scenario(db_session, entity_a.id, acct1.id, "PAYGROUP-IND1")
    await _seed_group_scenario(db_session, entity_a.id, acct2.id, "PAYGROUP-IND2")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    r1 = await _create_ready_run(client, headers, entity_a.id, acct1.id)
    r2 = await _create_ready_run(client, headers, entity_a.id, acct2.id)
    results = await asyncio.gather(
        client.post(f"/api/v1/reconciliation/runs/{r1}/execute", headers=headers),
        client.post(f"/api/v1/reconciliation/runs/{r2}/execute", headers=headers),
    )
    assert all(r.status_code == 200 and r.json()["advanced_match_count"] == 1 for r in results)


# ---------------------------------------------------------------------------
# Cross-group isolation and match-group endpoint authorization
# ---------------------------------------------------------------------------

async def test_cross_group_match_groups_endpoint_and_matching_isolated(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.entity import Group, LegalEntity
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="9999000055")

    group2 = Group(name="Adv5D Group 2", code="ADV5DG2", reporting_currency_code="NGN")
    db_session.add(group2)
    await db_session.flush()
    entity_c = LegalEntity(
        group_id=group2.id, name="Adv5D Entity C", code="ADV5DENTC", functional_currency_code="NGN", country="NG",
    )
    db_session.add(entity_c)
    await db_session.flush()
    account_c = await _make_account(db_session, entity_c.id, bank.id, account_number="9999000066")

    user_a = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group.id, label="adv5dgrp1",
    )
    user_c = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_c.id, label="adv5dgrp2",
    )
    await db_session.flush()
    # Same reference in both groups; group 2 owns the full grouping.
    await _seed_group_scenario(db_session, entity_c.id, account_c.id, "PAYGROUP-XGRP")
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, amount="10000000", bank_reference="PAYGROUP-XGRP",
    )
    await db_session.commit()

    headers_c = {"Authorization": f"Bearer {await _login(client, user_c)}"}
    run_c = await _create_ready_run(client, headers_c, entity_c.id, account_c.id)
    exec_c = await client.post(f"/api/v1/reconciliation/runs/{run_c}/execute", headers=headers_c)
    assert exec_c.json()["advanced_match_count"] == 1

    headers_a = {"Authorization": f"Bearer {await _login(client, user_a)}"}
    # Group 1's user cannot read Group 2's match groups.
    denied = await client.get(f"/api/v1/reconciliation/runs/{run_c}/match-groups", headers=headers_a)
    assert denied.status_code == 403

    # And Group 1's own run never pulls in Group 2's ledger rows.
    run_a = await _create_ready_run(client, headers_a, entity_a.id, account_a.id)
    exec_a = await client.post(f"/api/v1/reconciliation/runs/{run_a}/execute", headers=headers_a)
    assert exec_a.json()["advanced_match_count"] == 0


async def test_full_run_at_scale_10k_ledger_1k_bank_stays_bounded(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types, capsys,
):
    """
    SECTION 33: 10,000 TreasuryTransactions + 1,000 bank transactions in
    one run scope. Only one reference-linked grouping genuinely exists.
    Verifies the whole execute (Stage 5C + 5D) completes, finds exactly
    that one group, and never produces a group from the 10,000 unrelated
    rows. Elapsed time is printed for information only - it is not a
    benchmark claim and nothing asserts on it.
    """
    import time

    from app.models.bank_statement import BankStatementEntryType, BankStatementTransaction
    from app.models.lookup import CashDirection
    from app.models.rbac import EntityScopeType
    from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="9999000077")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="adv5dscale",
    )
    await db_session.flush()
    batch = await __import__("tests.test_reconciliation_matching_engine", fromlist=["x"])._make_import_batch(
        db_session, entity_a.id, None,
    )

    db_session.add_all([
        TreasuryTransaction(
            legal_entity_id=entity_a.id, event_type_code="INVESTMENT_MATURITY", direction=CashDirection.INFLOW,
            event_date=datetime.date(2026, 6, 15), transaction_currency_code="NGN",
            transaction_amount=Decimal(1000) + i, bank_account_id=account.id, reference=f"LEDGER-NOISE-{i}",
            status=TransactionStatus.POSTED,
        )
        for i in range(10000)
    ])
    db_session.add_all([
        BankStatementTransaction(
            legal_entity_id=entity_a.id, bank_id=bank.id, bank_account_id=account.id,
            statement_period_start=datetime.date(2026, 6, 1), statement_period_end=datetime.date(2026, 6, 30),
            transaction_date=datetime.date(2026, 6, 15), entry_type=BankStatementEntryType.CREDIT,
            amount=Decimal(9000000) + i, currency_code="NGN", bank_reference=f"BANK-NOISE-{i}",
            duplicate_key=f"scale-{i}", has_strong_identity=True, import_batch_id=batch.id, source_row_number=i + 2,
        )
        for i in range(1000)
    ])
    await db_session.flush()
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="500000000", bank_reference="PAYGROUP-SCALEGRP",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="200000000", direction="INFLOW",
        reference="PAYGROUP-SCALEGRP",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="300000000", direction="INFLOW",
        reference="PAYGROUP-SCALEGRP",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    started = time.monotonic()
    resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    elapsed = time.monotonic() - started
    with capsys.disabled():
        print(f"\n[scale test] 10,000 ledger + 1,001 bank rows: execute took {elapsed:.1f}s")

    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "COMPLETED"
    assert body["statement_transaction_count"] == 1001
    assert body["advanced_match_count"] == 1  # only the genuinely reference-linked group
