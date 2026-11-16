"""
Stage 5F authorization: scope is derived on the server from the caller's permissions. Request parameters can only
narrow a result; they can never widen it.
"""
import json
import re

import pytest

from tests.reconciliation_reports_world import build_world, kpis, report, table_rows
from tests.test_reconciliation_data_model import _make_scoped_user
from tests.test_reconciliation_open_items import _headers, _limited_user

ALL = ["overview", "run-summary", "matching-performance", "exceptions", "ageing", "assignment", "bank-accounts", "entity-dashboard", "group-dashboard"]
EXPORTABLE = ALL[1:]


async def _two_entities(client, db_session, demo):
    group_id = demo[0].id
    a = await build_world(client, db_session, demo, "ea", "9910000011", entity_index=0)
    b = await build_world(client, db_session, demo, "eb", "9910000021", entity_index=1)
    return group_id, a, b


@pytest.mark.parametrize("name", ALL)
async def test_entity_a_never_sees_entity_b_in_any_report(client, db_session, demo_group_and_entities, cash_event_types, name):
    _, a, b = await _two_entities(client, db_session, demo_group_and_entities)
    for mine, theirs in ((a, b), (b, a)):
        resp = await report(client, mine["headers"], name)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        blob = json.dumps(body)
        assert body["scope"]["entities"] == [f"{mine['entity_name']} ({mine['entity_code']})"]
        assert theirs["entity_name"] not in blob and theirs["entity_code"] not in blob
        # The other entity's transaction references look like "eb-B1" / "ea-L3". The uppercase B/L means this can never match
        # inside a (lowercase-hex) UUID; a plain "eb-" substring did, by chance, in roughly 9-17% of runs.
        assert not re.search(rf"{'eb' if mine is a else 'ea'}-[BL]\d", blob)


async def test_entity_scoped_numbers_equal_the_single_entity_numbers(client, db_session, demo_group_and_entities, cash_event_types):
    _, a, b = await _two_entities(client, db_session, demo_group_and_entities)
    for w in (a, b):
        k = kpis((await report(client, w["headers"], "entity-dashboard")).json())
        assert (k["accounts"], k["eligible"], k["accepted"], k["outstanding"], k["completion"]) == (1, 13, 6, 5, "46.15")   # not 2, 26, 12, 10
        exc = (await report(client, w["headers"], "exceptions")).json()
        assert kpis(exc)["exceptions"] == 5 and {r["entity"] for r in table_rows(exc, "exceptions")} == {w["entity_name"]}


async def test_entity_scoped_user_gets_no_group_level_totals_for_unauthorized_entities(client, db_session, demo_group_and_entities, cash_event_types):
    _, a, b = await _two_entities(client, db_session, demo_group_and_entities)
    body = (await report(client, a["headers"], "group-dashboard")).json()
    assert body["level"] == "GROUP"
    [g] = table_rows(body, "groups")
    assert g["entities"] == 1 and g["accounts"] == 1 and g["eligible"] == 13                       # only the authorized entity
    assert [r["entity"] for r in table_rows(body, "entities")] == [a["entity_name"]]
    assert kpis(body)["accounts"] == 1 and b["entity_name"] not in json.dumps(body)


async def test_group_authorized_user_sees_exactly_the_authorized_entities(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import EntityScopeType

    group_id, a, b = await _two_entities(client, db_session, demo_group_and_entities)
    group_user = await _make_scoped_user(db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group_id, label="grp")
    await db_session.commit()
    headers = await _headers(client, group_user)
    body = (await report(client, headers, "group-dashboard")).json()
    [g] = table_rows(body, "groups")
    assert (g["entities"], g["accounts"], g["eligible"], g["accepted"], g["outstanding"], g["completion_rate"]) == (2, 2, 26, 12, 10, "46.15")
    assert {r["entity"] for r in table_rows(body, "entities")} == {a["entity_name"], b["entity_name"]}
    for r in table_rows(body, "entities"):
        assert (r["eligible"], r["accepted"], r["outstanding"], r["completion_rate"]) == (13, 6, 5, "46.15")
    # the group user's entity filter narrows, and an entity outside RBAC would be refused (checked below)
    only_a = (await report(client, headers, "exceptions", legal_entity_id=str(a["entity"]))).json()
    assert {r["entity"] for r in table_rows(only_a, "exceptions")} == {a["entity_name"]}


async def test_direct_api_requests_cannot_bypass_entity_scope(client, db_session, demo_group_and_entities, cash_event_types):
    _, a, b = await _two_entities(client, db_session, demo_group_and_entities)
    h = a["headers"]
    for name in ALL:
        denied = await report(client, h, name, legal_entity_id=str(b["entity"]))
        assert denied.status_code == 403, name                                                      # B's entity id is refused, not trusted
    for name in ("exceptions", "bank-accounts", "run-summary", "ageing"):
        assert (await report(client, h, name, bank_account_id=str(b["account"]))).status_code == 404   # B's account looks nonexistent
    missing = await report(client, h, "exceptions", bank_account_id="00000000-0000-0000-0000-00000000dead")
    other = await report(client, h, "exceptions", bank_account_id=str(b["account"]))
    assert missing.status_code == other.status_code == 404 and missing.json() == other.json()      # indistinguishable: no enumeration
    mixed = await report(client, h, "exceptions", legal_entity_id=str(a["entity"]), bank_account_id=str(b["account"]))
    assert mixed.status_code == 404
    own = await report(client, h, "exceptions", legal_entity_id=str(a["entity"]), bank_account_id=str(a["account"]))
    assert own.status_code == 200 and kpis(own.json())["exceptions"] == 5


async def test_filters_cannot_expand_authorization_scope(client, db_session, demo_group_and_entities, cash_event_types):
    _, a, b = await _two_entities(client, db_session, demo_group_and_entities)
    h = a["headers"]
    for params in ({"assigned_to_user_id": str(b["user_id"])}, {"category": ["BANK_ONLY", "LEDGER_ONLY"]},
                   {"exception_status": ["OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", "REJECTED"]}, {"currency_code": "NGN"},
                   {"txn_date_from": "2000-01-01", "txn_date_to": "2999-12-31"}, {"min_age_days": 0, "max_age_days": 100000}, {"page_size": 200}):
        body = (await report(client, h, "exceptions", **params)).json()
        assert {r["entity"] for r in table_rows(body, "exceptions")} <= {a["entity_name"]}, params
    assert (await report(client, h, "exceptions", assigned_to_user_id=str(b["user_id"]))).json()["tables"][0]["total_rows"] == 0


async def test_view_and_export_permissions_are_enforced_server_side(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import TreasuryAction

    _, a, _ = await _two_entities(client, db_session, demo_group_and_entities)
    view_only = await _limited_user(db_session, a["entity"], [TreasuryAction.VIEW], "viewer")
    export_only = await _limited_user(db_session, a["entity"], [TreasuryAction.EXPORT], "exporter")
    nothing = await _limited_user(db_session, a["entity"], [TreasuryAction.COMMENT], "nobody")
    await db_session.commit()
    vh, eh, nh = await _headers(client, view_only), await _headers(client, export_only), await _headers(client, nothing)
    for name in ALL:
        assert (await report(client, vh, name)).status_code == 200            # VIEW is enough to read
        assert (await report(client, nh, name)).status_code == 403            # no VIEW: nothing
        assert (await report(client, eh, name)).status_code == 403            # EXPORT alone does not allow reading
    for name in EXPORTABLE:
        base = f"/api/v1/reconciliation/reports/{name}/export"
        params = {"date_from": "2026-06-01", "date_to": "2026-06-30", "format": "xlsx"}
        assert (await client.get(base, params=params, headers=vh)).status_code == 403    # VIEW without EXPORT cannot download
        assert (await client.get(base, params=params, headers=nh)).status_code == 403
        assert (await client.get(base, params=params, headers=eh)).status_code == 403    # EXPORT without VIEW cannot either
    assert (await client.get("/api/v1/reconciliation/reports/exceptions")).status_code in (401, 403)   # unauthenticated


async def test_reports_are_never_cached(client, db_session, demo_group_and_entities, cash_event_types):
    _, a, b = await _two_entities(client, db_session, demo_group_and_entities)
    resp_a = await report(client, a["headers"], "exceptions")
    assert resp_a.headers["cache-control"] == "no-store"
    first = (await report(client, a["headers"], "exceptions")).json()
    second = (await report(client, b["headers"], "exceptions")).json()
    assert {r["entity"] for r in table_rows(first, "exceptions")} == {a["entity_name"]}
    assert {r["entity"] for r in table_rows(second, "exceptions")} == {b["entity_name"]}      # same URL, different user, different data
