import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_advanced_matching import _make_fx_rate


async def test_exact_date_rate_selected(db_session: AsyncSession, demo_group_and_entities):
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 9, 15))
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert selection is not None
    assert selection.fx_rate.rate_date == datetime.date(2026, 9, 15)
    assert selection.fx_rate.rate == Decimal("1600.00")
    assert selection.inverse_used is False


async def test_prior_date_fallback_selected(db_session: AsyncSession, demo_group_and_entities):
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1590.00", datetime.date(2026, 9, 10))
    await _make_fx_rate(db_session, "USD", "NGN", "1595.00", datetime.date(2026, 9, 14))
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert selection is not None
    assert selection.fx_rate.rate_date == datetime.date(2026, 9, 14)


async def test_future_rate_never_selected(db_session: AsyncSession, demo_group_and_entities):
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 9, 20))
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert selection is None


async def test_stale_rate_rejected_when_max_staleness_configured(db_session: AsyncSession, demo_group_and_entities):
    from app.models.reconciliation import ReconciliationConfiguration
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 9, 10))
    config = ReconciliationConfiguration(
        version=1, is_current=True, is_active=True, effective_from=datetime.date.today(),
        matching_rule_config={"fx_max_staleness_days": 3},
    )
    db_session.add(config)
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), config)
    assert selection is None


async def test_rate_within_staleness_limit_accepted(db_session: AsyncSession, demo_group_and_entities):
    from app.models.reconciliation import ReconciliationConfiguration
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 9, 13))
    config = ReconciliationConfiguration(
        version=1, is_current=True, is_active=True, effective_from=datetime.date.today(),
        matching_rule_config={"fx_max_staleness_days": 3},
    )
    db_session.add(config)
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), config)
    assert selection is not None


async def test_no_staleness_limit_invented_when_not_configured(db_session: AsyncSession, demo_group_and_entities):
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 1, 1))
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert selection is not None


async def test_configured_rate_type_selected_deterministically(db_session: AsyncSession, demo_group_and_entities):
    from app.models.currency import FXRateType
    from app.models.reconciliation import ReconciliationConfiguration
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 9, 10), rate_type=FXRateType.SPOT)
    await _make_fx_rate(
        db_session, "USD", "NGN", "1550.00", datetime.date(2026, 9, 10), rate_type=FXRateType.TREASURY,
    )
    await _make_fx_rate(
        db_session, "USD", "NGN", "1500.00", datetime.date(2026, 9, 10), rate_type=FXRateType.MANAGEMENT,
    )
    config = ReconciliationConfiguration(
        version=1, is_current=True, is_active=True, effective_from=datetime.date.today(),
        matching_rule_config={"fx_rate_type": "TREASURY"},
    )
    db_session.add(config)
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), config)
    assert selection is not None
    assert selection.fx_rate.rate == Decimal("1550.00")
    assert selection.fx_rate.rate_type == FXRateType.TREASURY


async def test_default_rate_type_is_spot_when_unconfigured(db_session: AsyncSession, demo_group_and_entities):
    from app.models.currency import FXRateType
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 9, 10), rate_type=FXRateType.SPOT)
    await _make_fx_rate(
        db_session, "USD", "NGN", "1550.00", datetime.date(2026, 9, 10), rate_type=FXRateType.TREASURY,
    )
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert selection is not None
    assert selection.fx_rate.rate == Decimal("1600.00")


async def test_currency_direction_usd_to_ngn_and_ngn_to_usd_independent(
    db_session: AsyncSession, demo_group_and_entities,
):
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 9, 10))
    await db_session.commit()

    forward = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert forward is not None
    assert forward.fx_rate.rate == Decimal("1600.00")

    reverse = await select_fx_rate(db_session, "NGN", "USD", datetime.date(2026, 9, 15), None)
    assert reverse is None


async def test_inverse_rate_not_used_when_only_reverse_pair_exists(
    db_session: AsyncSession, demo_group_and_entities,
):
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "NGN", "USD", "0.000625", datetime.date(2026, 9, 10))
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert selection is None


async def test_triangulation_not_performed(db_session: AsyncSession, demo_group_and_entities):
    from app.services.reconciliation_advanced_matching import select_fx_rate

    await _make_fx_rate(db_session, "USD", "EUR", "0.92", datetime.date(2026, 9, 10))
    await _make_fx_rate(db_session, "EUR", "NGN", "1740.00", datetime.date(2026, 9, 10))
    await db_session.commit()

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert selection is None


async def test_no_fx_rate_at_all_returns_none(db_session: AsyncSession, demo_group_and_entities):
    from app.services.reconciliation_advanced_matching import select_fx_rate

    selection = await select_fx_rate(db_session, "USD", "NGN", datetime.date(2026, 9, 15), None)
    assert selection is None


async def test_decimal_conversion_precision(db_session: AsyncSession, demo_group_and_entities):
    from decimal import Decimal as D

    rate = D("1583.3333333333")
    amount = D(10000)
    converted = (amount * rate).quantize(D("0.01"))
    assert isinstance(converted, D)
    assert converted == D("15833333.33")


async def test_historical_reproducibility_newer_rate_does_not_change_existing_group(
    client, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from sqlalchemy import select as sa_select

    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import MatchRelationshipType, ReconciliationMatchGroup
    from tests.test_reconciliation_data_model import _login, _make_scoped_user
    from tests.test_reconciliation_matching_engine import (
        _create_ready_run,
        _make_bank_statement_txn,
        _make_treasury_txn,
    )

    group_obj, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number="8888000011")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="fxhistoricalrepro",
    )
    await db_session.flush()

    old_rate = await _make_fx_rate(db_session, "USD", "NGN", "1600.00", datetime.date(2026, 6, 1))
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="USD", amount="10000",
        txn_date="2026-06-10", bank_reference="FX-HIST",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="NGN", amount="16000000",
        event_date="2026-06-10", direction="INFLOW", reference="FX-HIST",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)

    result = await db_session.execute(
        sa_select(ReconciliationMatchGroup).where(
            ReconciliationMatchGroup.relationship_type == MatchRelationshipType.FX_MATCH,
        )
    )
    fx_group = result.scalars().first()
    assert fx_group is not None
    assert fx_group.fx_rate_id == old_rate.id
    assert fx_group.fx_rate == Decimal("1600.0000000000")

    await _make_fx_rate(db_session, "USD", "NGN", "1650.00", datetime.date(2026, 6, 15))
    await db_session.commit()

    refreshed = await db_session.get(ReconciliationMatchGroup, fx_group.id)
    assert refreshed.fx_rate_id == old_rate.id
    assert refreshed.fx_rate == Decimal("1600.0000000000")
