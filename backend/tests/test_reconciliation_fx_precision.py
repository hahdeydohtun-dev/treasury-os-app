import datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_advanced_matching import _get_match_groups, _make_fx_rate
from tests.test_reconciliation_data_model import _login, _make_scoped_user, _run_payload
from tests.test_reconciliation_matching_engine import _make_bank_statement_txn, _make_treasury_txn

# ---------------------------------------------------------------------------
# Pure conversion helper: precision comes from the target currency
# ---------------------------------------------------------------------------

def test_zero_decimal_currency_rounds_to_whole_units_half_up():
    from app.services.reconciliation_advanced_matching import convert_fx_amount

    assert convert_fx_amount(Decimal(100), Decimal("12.3456"), 0) == Decimal(1235)  # 1234.56
    assert convert_fx_amount(Decimal(1000), Decimal("150.1234"), 0) == Decimal(150123)  # 150123.4
    assert convert_fx_amount(Decimal(1), Decimal("2.5"), 0) == Decimal(3)  # HALF_UP, not banker's 2
    assert convert_fx_amount(Decimal(1), Decimal("2.5"), 0).as_tuple().exponent == 0


def test_two_decimal_currency_behaviour_unchanged():
    from app.services.reconciliation_advanced_matching import convert_fx_amount

    assert convert_fx_amount(Decimal(10000), Decimal("1600.00"), 2) == Decimal("16000000.00")
    assert convert_fx_amount(Decimal(10000), Decimal("1583.3333333333"), 2) == Decimal("15833333.33")


def test_three_decimal_currency_retains_three_places():
    from app.services.reconciliation_advanced_matching import convert_fx_amount

    result = convert_fx_amount(Decimal(1000), Decimal("0.3072345"), 3)
    assert result == Decimal("307.235")  # 307.2345 -> HALF_UP
    assert result.as_tuple().exponent == -3


def test_four_decimal_currency_retains_four_places():
    from app.services.reconciliation_advanced_matching import convert_fx_amount

    result = convert_fx_amount(Decimal(1000), Decimal("0.30723456"), 4)
    assert result == Decimal("307.2346")
    assert result.as_tuple().exponent == -4


def test_conversion_stays_decimal_throughout():
    from app.services.reconciliation_advanced_matching import convert_fx_amount

    assert isinstance(convert_fx_amount(Decimal(1), Decimal("1.1"), 2), Decimal)


# ---------------------------------------------------------------------------
# Integration: precision of the LEDGER (target) currency drives the match
# ---------------------------------------------------------------------------

async def _ensure_currency(db_session, code, decimal_places):
    from app.models.currency import Currency

    existing = await db_session.get(Currency, code)
    if existing is None:
        db_session.add(Currency(code=code, name=f"{code} test", decimal_places=decimal_places))
    else:
        existing.decimal_places = decimal_places
    await db_session.flush()


async def _run_fx_scenario(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, *, label, account_number,
    target_currency, target_dp, rate, bank_amount, ledger_amount, fx_tolerance_pct, rate_date="2026-06-10",
    seed_rate=True,
):
    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationConfiguration

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, currency="USD", account_number=account_number)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label=label,
    )
    await _ensure_currency(db_session, target_currency, target_dp)
    if seed_rate:
        await _make_fx_rate(db_session, "USD", target_currency, rate, datetime.date.fromisoformat(rate_date))
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="USD", amount=bank_amount,
        bank_reference=f"FXP-{label}",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency=target_currency,
        amount=ledger_amount, direction="INFLOW", reference=f"FXP-{label}",
    )
    config = ReconciliationConfiguration(
        legal_entity_id=entity_a.id, version=1, is_current=True, is_active=True,
        effective_from=datetime.date.today(), matching_rule_config={"fx_tolerance_pct": fx_tolerance_pct},
    )
    db_session.add(config)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    created = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_a.id, account.id, configuration_id=config.id), headers=headers,
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    executed = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert executed.status_code == 200
    groups = [g for g in await _get_match_groups(client, headers, run_id) if g["relationship_type"] == "FX_MATCH"]
    return executed.json(), groups, headers, run_id


async def test_zero_decimal_target_currency_exact_match_at_zero_tolerance(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    # 1000 USD * 150.1234 = 150123.4 -> whole yen 150123. With zero tolerance
    # the ledger 150123.00 matches ONLY if conversion was quantized to 0 dp.
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="jpymatch", account_number="5151000011",
        target_currency="JPY", target_dp=0, rate="150.1234", bank_amount="1000", ledger_amount="150123",
        fx_tolerance_pct="0",
    )
    assert body["advanced_match_count"] == 1
    assert len(groups) == 1 and groups[0]["difference"] == "0.00"
    assert "= 150123 JPY" in groups[0]["reason"]  # whole units, no decimals


async def test_zero_decimal_target_currency_adjacent_unit_does_not_match_at_zero_tolerance(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="jpynomatch", account_number="5151000022",
        target_currency="JPY", target_dp=0, rate="150.1234", bank_amount="1000", ledger_amount="150124",
        fx_tolerance_pct="0",
    )
    assert body["advanced_match_count"] == 0 and groups == []


async def test_two_decimal_target_currency_regression(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="ngnreg", account_number="5151000033",
        target_currency="NGN", target_dp=2, rate="1583.3333333333", bank_amount="10000",
        ledger_amount="15833333.33", fx_tolerance_pct="0",
    )
    assert body["advanced_match_count"] == 1
    assert "= 15833333.33 NGN" in groups[0]["reason"]


async def test_three_decimal_target_currency_is_not_rounded_to_two_places(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    # 1000 USD * 0.3072345 = 307.2345 -> 307.235 (3 dp). Ledger amounts are
    # stored at 2 dp (307.24). Under the OLD hardcoded 2-dp rounding the
    # converted value would have been 307.23/307.24 and matched at zero
    # tolerance; correctly keeping 3 dp, 307.235 != 307.24 -> NO match.
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="kwdnomatch", account_number="5151000044",
        target_currency="KWD", target_dp=3, rate="0.3072345", bank_amount="1000", ledger_amount="307.24",
        fx_tolerance_pct="0",
    )
    assert body["advanced_match_count"] == 0 and groups == []


async def test_three_decimal_target_currency_matches_within_tolerance_on_quantized_value(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="kwdmatch", account_number="5151000055",
        target_currency="KWD", target_dp=3, rate="0.3072345", bank_amount="1000", ledger_amount="307.24",
        fx_tolerance_pct="0.01",
    )
    assert body["advanced_match_count"] == 1
    assert "= 307.235 KWD" in groups[0]["reason"]  # three decimals retained in the recorded conversion


async def test_tolerance_is_evaluated_on_the_quantized_amount(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    # Unquantized 150123.4 vs ledger 150123 differs by 0.4, which exceeds a
    # 0.0001% tolerance (allowed 0.15). It matches only because the amount is
    # quantized to whole yen FIRST (diff 0), then tolerance is applied.
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="tolquant", account_number="5151000066",
        target_currency="JPY", target_dp=0, rate="150.1234", bank_amount="1000", ledger_amount="150123",
        fx_tolerance_pct="0.0001",
    )
    assert body["advanced_match_count"] == 1
    assert groups[0]["fx_tolerance_pct"] == "0.000" or Decimal(groups[0]["fx_tolerance_pct"]) == Decimal("0.0001")


async def test_missing_fx_rate_still_produces_no_match_for_non_two_decimal_currency(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    body, groups, _, _ = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="jpynorate", account_number="5151000077",
        target_currency="JPY", target_dp=0, rate="150.1234", bank_amount="1000", ledger_amount="150123",
        fx_tolerance_pct="0", seed_rate=False,
    )
    assert body["advanced_match_count"] == 0 and groups == []


async def test_historical_fx_result_unchanged_after_currency_precision_and_rate_change(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.currency import Currency
    from app.models.reconciliation import ReconciliationMatchGroup

    body, groups, headers, run_id = await _run_fx_scenario(
        client, db_session, demo_group_and_entities, label="jpyhist", account_number="5151000088",
        target_currency="JPY", target_dp=0, rate="150.1234", bank_amount="1000", ledger_amount="150123",
        fx_tolerance_pct="0",
    )
    assert body["advanced_match_count"] == 1
    group_id = groups[0]["id"]
    before = await db_session.get(ReconciliationMatchGroup, group_id)
    snapshot = (before.fx_rate_id, before.fx_rate, before.fx_rate_date, before.fx_rate_type, before.reason,
                before.ledger_aggregate_amount, before.difference)

    # Configuration changes later: currency precision edited and a newer rate added.
    (await db_session.get(Currency, "JPY")).decimal_places = 2
    await _make_fx_rate(db_session, "USD", "JPY", "160.0000", datetime.date(2026, 6, 20))
    await db_session.commit()

    db_session.expire_all()
    after = await db_session.get(ReconciliationMatchGroup, group_id)
    assert (after.fx_rate_id, after.fx_rate, after.fx_rate_date, after.fx_rate_type, after.reason,
            after.ledger_aggregate_amount, after.difference) == snapshot
