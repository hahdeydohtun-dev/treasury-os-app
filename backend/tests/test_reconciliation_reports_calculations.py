"""Stage 5F calculation tests: every figure is checked against an independently derived expectation."""
import datetime

import pytest

from tests.reconciliation_reports_world import (
    build_world,
    items_by_ref,
    kpis,
    report,
    table_rows,
)

ALL_REPORTS = ["overview", "run-summary", "matching-performance", "exceptions", "ageing", "assignment", "bank-accounts",
               "entity-dashboard", "group-dashboard"]


async def test_every_report_runs_against_postgres_and_has_the_common_structure(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "smoke", "9900000011")
    for name in ALL_REPORTS:
        resp = await report(client, w["headers"], name, as_of="2026-07-20")
        assert resp.status_code == 200, (name, resp.text[:300])
        body = resp.json()
        assert body["report"] == name and body["timezone"] == "UTC" and body["generated_at"]
        assert {"kpis", "tables", "definitions", "scope", "filters", "level", "reporting_period"} <= set(body)
        assert body["definitions"], name                                   # methodology is always stated
        assert body["scope"]["entities"] == [f"{w['entity_name']} ({w['entity_code']})"]


# ======================================================================================
# helpers
# ======================================================================================
def _find(items, category, amount, currency="NGN"):
    return next(i for i in items.values() if i["category"] == category and i["amount"] == amount and i["currency_code"] == currency)


async def _colleague(db_session, entity_id, label):
    from app.models.rbac import EntityScopeType
    from tests.test_reconciliation_data_model import _make_scoped_user

    user = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_id, label=label)
    user.test_id = user.id
    await db_session.commit()
    return user.test_id


async def _act(client, headers, item_id, action, body=None):
    resp = await client.post(f"/api/v1/reconciliation/open-items/{item_id}/{action}", json=body or {}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ======================================================================================
# run summary: a transaction, a proposal, an accepted match and an exception are four different things
# ======================================================================================
async def test_run_summary_counts_each_kind_of_thing_separately(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "rs", "9900000021")
    body = (await report(client, w["headers"], "run-summary")).json()
    [row] = table_rows(body, "runs")
    assert (row["bank_in_scope"], row["ledger_in_scope"]) == (6, 7)            # B6 reversed and B7 (July) are excluded
    assert (row["accepted_bank"], row["accepted_ledger"]) == (2, 4)            # B1,B2 / L1..L4
    assert (row["proposed_bank"], row["proposed_ledger"]) == (1, 1)            # B3 / L5: pending proposal, NOT accepted
    assert (row["unmatched_bank"], row["unmatched_ledger"]) == (3, 2)          # B4,B5,B8 / L6,L7
    assert row["pending_proposals"] == 1 and row["rejected_proposals"] == 1    # S2 / S3
    assert (row["exceptions_open"], row["exceptions_under_review"], row["exceptions_resolved"], row["exceptions_closed"]) == (5, 0, 0, 0)
    assert row["bank_in_scope"] == row["accepted_bank"] + row["proposed_bank"] + row["unmatched_bank"]       # one bucket each
    assert row["ledger_in_scope"] == row["accepted_ledger"] + row["proposed_ledger"] + row["unmatched_ledger"]
    assert row["currency"] == "NGN" and row["status"] == "COMPLETED" and row["reference"].startswith("RUN-")
    assert row["bank_account"].endswith("0021") or "*" in row["bank_account"]    # masked, never the full number


async def test_pending_proposals_are_not_counted_as_accepted_and_rejected_are_not_matches(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "pend", "9900000031")
    perf = (await report(client, w["headers"], "matching-performance")).json()
    k = kpis(perf)
    assert (k["eligible"], k["accepted"], k["proposed"], k["unmatched"]) == (13, 6, 2, 5)
    [one_to_one] = [r for r in table_rows(perf, "types") if r["type"] == "One-to-one"]
    assert one_to_one["accepted_decisions"] == 1       # S1 only: S2 is pending and S3 was rejected
    assert one_to_one["pending_proposals"] == 1 and one_to_one["rejected_proposals"] == 1
    assert one_to_one["accepted_unique_bank"] == 1 and one_to_one["accepted_unique_ledger"] == 1
    # the rejected pair's transactions (B4, L6) are unmatched, not matched; the pending pair's (B3, L5) are 'proposed only'
    buckets = {r["side"]: r for r in table_rows(perf, "buckets")}
    assert buckets["Bank statement rows"]["unmatched"] == 3 and buckets["Bank statement rows"]["proposed"] == 1


async def test_no_double_counting_when_a_transaction_is_in_two_accepted_proposals(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "dbl", "9900000041")
    perf = (await report(client, w["headers"], "matching-performance")).json()
    types = {r["type"]: r for r in table_rows(perf, "types")}
    assert types["One-to-many"]["accepted_unique_ledger"] == 2 and types["Internal transfer"]["accepted_unique_ledger"] == 2
    per_type_sum = sum(r["accepted_unique_ledger"] for r in types.values())
    buckets = {r["side"]: r for r in table_rows(perf, "buckets")}
    # L3 belongs to BOTH the accepted one-to-many group and the accepted internal-transfer group:
    assert per_type_sum == 5                                    # naive addition of the per-type figures would overstate...
    assert buckets["Ledger rows"]["accepted"] == 4              # ...the de-duplicated count of unique ledger transactions
    assert types["One-to-many"]["accepted_decisions"] + types["Internal transfer"]["accepted_decisions"] + types["One-to-one"]["accepted_decisions"] == 3
    assert any("not mutually exclusive" in d or "not additive" in d.lower() or "must not be added" in d for d in perf["definitions"])
    assert all(types[t]["accepted_decisions"] == 0 for t in ("Many-to-one", "Batch payment", "FX-aware"))   # none of these exist here


async def test_unique_transaction_and_completion_figures(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "uniq", "9900000051")
    perf = (await report(client, w["headers"], "matching-performance")).json()
    buckets = {r["side"]: r for r in table_rows(perf, "buckets")}
    assert (buckets["Bank statement rows"]["total"], buckets["Bank statement rows"]["accepted"]) == (6, 2)
    assert (buckets["Ledger rows"]["total"], buckets["Ledger rows"]["accepted"]) == (7, 4)
    both = buckets["Both (de-duplicated total)"]
    assert (both["total"], both["accepted"], both["proposed"], both["unmatched"]) == (13, 6, 2, 5)
    assert both["accepted"] + both["proposed"] + both["unmatched"] == both["total"]
    assert kpis(perf)["completion"] == "46.15"                  # 6 / 13 x 100, half-up to 2 dp


# ======================================================================================
# exception statuses, assignment and resolution time (from the real workflow events)
# ======================================================================================
async def _drive_workflow(client, db_session, w):
    """b4 stays OPEN unassigned; b5 assigned + UNDER_REVIEW; l6 RESOLVED; l7 CLOSED; b8 (USD) REJECTED."""
    h = w["headers"]
    items = await items_by_ref(client, h)
    it = {"b4": _find(items, "BANK_ONLY", "400.00"), "b5": _find(items, "BANK_ONLY", "500.00"), "b8": _find(items, "BANK_ONLY", "1000.00", "USD"),
          "l6": _find(items, "LEDGER_ONLY", "600.00"), "l7": _find(items, "LEDGER_ONLY", "700.00")}
    colleague = await _colleague(db_session, w["entity"], "colleague")
    await _act(client, h, it["b5"]["id"], "assign", {"assignee_user_id": str(colleague), "reason": "owns this account"})
    await _act(client, h, it["b5"]["id"], "start-investigation")
    for key in ("l6", "l7"):
        await _act(client, h, it[key]["id"], "start-investigation")
        await _act(client, h, it[key]["id"], "resolve", {"reason": "Investigated with the bank; timing difference confirmed."})
    await _act(client, h, it["l7"]["id"], "close", {"note": "Confirmed."})
    await _act(client, h, it["b8"]["id"], "reject", {"reason": "Duplicate of an item tracked elsewhere."})
    return it, colleague


async def test_exception_status_counts_follow_the_workflow(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "st", "9900000061")
    await _drive_workflow(client, db_session, w)
    [row] = table_rows((await report(client, w["headers"], "run-summary")).json(), "runs")
    assert (row["exceptions_open"], row["exceptions_under_review"], row["exceptions_resolved"], row["exceptions_closed"]) == (1, 1, 1, 1)
    listed = (await report(client, w["headers"], "exceptions")).json()
    assert kpis(listed)["exceptions"] == 2                                       # default: OPEN + UNDER_REVIEW only
    everything = (await report(client, w["headers"], "exceptions", exception_status=["OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", "REJECTED"])).json()
    assert kpis(everything)["exceptions"] == 5
    assert sorted(r["status"] for r in table_rows(everything, "exceptions")) == ["CLOSED", "OPEN", "REJECTED", "RESOLVED", "UNDER_REVIEW"]


async def test_assignment_report_uses_the_recorded_events(client, db_session, demo_group_and_entities, cash_event_types):
    from sqlalchemy import select

    from app.models.rbac import User
    from app.models.reconciliation import (
        OpenItemEventType,
        ReconciliationOpenItemEvent,
    )
    from tests.reconciliation_reports_world import backdate

    w = await build_world(client, db_session, demo_group_and_entities, "as", "9900000071")
    it, colleague = await _drive_workflow(client, db_session, w)
    # give the two resolved items known durations: opened exactly 2 and 4 days before their (real) RESOLVED events
    for key, days in (("l6", 2), ("l7", 4)):
        resolved_at = (await db_session.execute(select(ReconciliationOpenItemEvent.created_at).where(
            ReconciliationOpenItemEvent.open_item_id == it[key]["id"], ReconciliationOpenItemEvent.event_type == OpenItemEventType.RESOLVED))).scalar_one()
        await backdate(db_session, it[key]["id"], resolved_at - datetime.timedelta(days=days))
    name = (await db_session.execute(select(User.full_name).where(User.id == colleague))).scalar_one()

    body = (await report(client, w["headers"], "assignment")).json()
    k = kpis(body)
    assert (k["assigned"], k["unassigned"], k["under_review"], k["resolved"], k["closed"]) == (1, 1, 1, 1, 1)
    assert k["no_activity"] == 1 and k["reopened"] == 0                          # only b4 has had no investigation event
    [res] = table_rows(body, "resolution")
    assert (res["measured"], res["avg_hours"], res["avg_days"]) == (2, "72.00", "3.00")   # mean of 48h and 96h
    by = {r["assignee"]: r for r in table_rows(body, "by_assignee")}
    assert by[name]["under_review"] == 1 and by[name]["total"] == 1
    assert (by["Unassigned"]["open"], by["Unassigned"]["resolved"], by["Unassigned"]["closed"], by["Unassigned"]["rejected"], by["Unassigned"]["total"]) == (1, 1, 1, 1, 4)
    assert len(table_rows(body, "oldest")) == 2 and len(table_rows(body, "no_activity")) == 1
    assert any("Unassignment is not a workflow action" in n for n in body["notes"])      # a limitation stated, not a fabricated metric

    await _act(client, w["headers"], it["l7"]["id"], "reopen", {"reason": "New statement shows a second related payment."})
    again = (await report(client, w["headers"], "assignment")).json()
    assert kpis(again)["reopened"] == 1 and kpis(again)["closed"] == 0
    [reopen] = table_rows(again, "reopened")
    assert (reopen["items"], reopen["events"]) == (1, 1)


async def test_average_resolution_time_is_undefined_not_zero_when_nothing_is_resolved(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "nr", "9900000081")
    body = (await report(client, w["headers"], "assignment")).json()
    [res] = table_rows(body, "resolution")
    assert res["measured"] == 0 and res["avg_hours"] is None and res["avg_days"] is None
    assert kpis(body)["avg_resolution_hours"] is None


# ======================================================================================
# ageing: bands, per-currency amounts, and status/assignee rebuilt from the event history
# ======================================================================================
async def test_ageing_bands_currency_totals_and_history_reconstruction(client, db_session, demo_group_and_entities, cash_event_types):
    from tests.reconciliation_reports_world import backdate

    w = await build_world(client, db_session, demo_group_and_entities, "ag", "9900000091")
    h = w["headers"]
    today = datetime.datetime.now(datetime.UTC).date()
    as_of_then = today - datetime.timedelta(days=1)                                  # "yesterday": before today's workflow events
    items = await items_by_ref(client, h)
    it = {"b4": _find(items, "BANK_ONLY", "400.00"), "b5": _find(items, "BANK_ONLY", "500.00"), "b8": _find(items, "BANK_ONLY", "1000.00", "USD"),
          "l6": _find(items, "LEDGER_ONLY", "600.00"), "l7": _find(items, "LEDGER_ONLY", "700.00")}
    for key, age in (("b4", 3), ("b5", 10), ("b8", 20), ("l6", 45), ("l7", 90)):       # one item in each of the five bands, as of yesterday
        await backdate(db_session, it[key]["id"], datetime.datetime.combine(as_of_then - datetime.timedelta(days=age), datetime.time(12, 0), tzinfo=datetime.UTC))
    colleague = await _colleague(db_session, w["entity"], "ag-col")
    await _act(client, h, it["b5"]["id"], "assign", {"assignee_user_id": str(colleague), "reason": "owns this account"})   # TODAY
    await _act(client, h, it["l7"]["id"], "start-investigation")
    await _act(client, h, it["l7"]["id"], "resolve", {"reason": "Investigated and confirmed as a timing difference."})
    await _act(client, h, it["l7"]["id"], "close", {"note": "Done."})                                                         # TODAY: now CLOSED

    then = (await report(client, h, "ageing", as_of=str(as_of_then))).json()
    bands = {r["band"]: r for r in table_rows(then, "bands")}
    assert [bands[b]["items"] for b in ("0-7 days", "8-14 days", "15-30 days", "31-60 days", "More than 60 days")] == [1, 1, 1, 1, 1]
    # as of yesterday NOTHING had been assigned or closed yet: history, not the current status, decides
    assert kpis(then)["outstanding"] == 5 and kpis(then)["unassigned"] == 5 and all(b["assigned"] == 0 for b in bands.values())
    assert kpis(then)["over60"] == 1 and kpis(then)["oldest"] == 90
    cur = {(r["band"], r["currency"]): r for r in table_rows(then, "band_currency")}
    assert cur[("0-7 days", "NGN")]["amount"] == "400.00" and cur[("15-30 days", "USD")]["amount"] == "1000.00"
    assert cur[("More than 60 days", "NGN")]["amount"] == "700.00"
    assert not any(r["amount"] == "3200.00" for r in cur.values())                  # NGN and USD are never added together
    [acct] = table_rows(then, "by_account")
    assert acct["total"] == 5 and acct["amounts"] == "NGN 2,200.00; USD 1,000.00"
    categories = {r["name"]: r for r in table_rows(then, "by_category")}
    assert categories["BANK_ONLY"]["total"] == 3 and categories["LEDGER_ONLY"]["total"] == 2

    now = (await report(client, h, "ageing", as_of=str(today))).json()              # today: l7 is closed, b5 is assigned
    assert kpis(now)["outstanding"] == 4 and kpis(now)["unassigned"] == 3
    nb = {r["band"]: r for r in table_rows(now, "bands")}
    assert nb["More than 60 days"]["items"] == 0                                      # l7 (the 90-day item) is closed today
    assert nb["8-14 days"]["assigned"] == 1 and sum(r["assigned"] for r in nb.values()) == 1   # b5 (age 11 today) was assigned today

    before_any = (await report(client, h, "ageing", as_of=str(as_of_then - datetime.timedelta(days=100)))).json()
    assert kpis(before_any)["outstanding"] == 0                                      # items created after the as-of date are excluded


async def test_ageing_band_boundaries(client, db_session, demo_group_and_entities, cash_event_types):
    from app.services.reconciliation_report_service import band_for

    assert [band_for(a) for a in (0, 7, 8, 14, 15, 30, 31, 60, 61, 500)] == ["0-7", "0-7", "8-14", "8-14", "15-30", "15-30", "31-60", "31-60", "60+", "60+"]


# ======================================================================================
# currency separation everywhere amounts appear
# ======================================================================================
async def test_amounts_are_never_added_across_currencies(client, db_session, demo_group_and_entities, cash_event_types):
    import json

    w = await build_world(client, db_session, demo_group_and_entities, "cur", "9900000101")
    exc = (await report(client, w["headers"], "exceptions")).json()
    totals = {r["currency"]: r["amount"] for r in table_rows(exc, "amounts")}
    assert totals == {"NGN": "2200.00", "USD": "1000.00"}                            # 400+500+600+700 and 1000, separately
    perf = (await report(client, w["headers"], "matching-performance")).json()
    cur = {r["currency"]: r for r in table_rows(perf, "currencies")}
    assert (cur["NGN"]["bank_total"], cur["NGN"]["bank_accepted"], cur["NGN"]["bank_proposed"], cur["NGN"]["bank_unmatched"]) == ("4200.00", "3000.00", "300.00", "900.00")
    assert (cur["NGN"]["ledger_total"], cur["NGN"]["ledger_accepted"], cur["NGN"]["ledger_proposed"], cur["NGN"]["ledger_unmatched"]) == ("5200.00", "3400.00", "500.00", "1300.00")
    assert (cur["USD"]["bank_total"], cur["USD"]["bank_unmatched"]) == ("1000.00", "1000.00")
    dash = (await report(client, w["headers"], "entity-dashboard")).json()
    [row] = table_rows(dash, "entities")
    assert row["amounts"] == "NGN 2,200.00; USD 1,000.00"
    for name in ("exceptions", "ageing", "matching-performance", "entity-dashboard"):
        blob = json.dumps((await report(client, w["headers"], name)).json())
        assert "3200.00" not in blob and "3,200.00" not in blob, name                # the cross-currency sum appears nowhere
    assert all(isinstance(v, str) for v in totals.values())                           # money is serialized as exact strings, not floats


# ======================================================================================
# bank accounts and dashboards: an account with no run is NOT YET RECONCILED, never "zero exceptions"
# ======================================================================================
async def _add_unreconciled_account(db_session, w):
    from tests.test_bank_statement_ingestion import _make_account, _make_bank
    from tests.test_reconciliation_matching_engine import (
        _make_bank_statement_txn,
        _make_treasury_txn,
    )

    account = await _make_account(db_session, w["entity"], (await _make_bank(db_session)).id, account_number="7766554433")
    await _make_bank_statement_txn(db_session, entity_id=w["entity"], account_id=account.id, amount="250", bank_reference="X-1", txn_date="2026-06-12", row_number=2)
    await _make_treasury_txn(db_session, entity_id=w["entity"], account_id=account.id, amount="250", direction="INFLOW", reference="X-2", event_date="2026-06-12")
    account_id = account.id
    await db_session.commit()
    return account_id


async def test_bank_account_report_distinguishes_not_yet_reconciled_from_zero(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "ba", "9900000111")
    unreconciled = await _add_unreconciled_account(db_session, w)
    body = (await report(client, w["headers"], "bank-accounts")).json()
    rows = {r["id"]: r for r in table_rows(body, "accounts")}
    done, pending = rows[str(w["account"])], rows[str(unreconciled)]
    assert done["state"] == "Reconciled" and (done["eligible"], done["accepted_bank"], done["accepted_ledger"]) == (13, 2, 4)
    assert (done["outstanding"], done["unassigned"], done["completion_rate"], done["completed_runs"]) == (5, 5, "46.15", 1)
    assert done["oldest_open_days"] == 0 and done["latest_run"].startswith("RUN-") and "COMPLETED" in done["latest_run"]
    # no run: represented as NOT YET RECONCILED - exception/accepted/completion are blank (None), not 0
    assert pending["state"] == "NOT YET RECONCILED" and pending["latest_run"] == "No run" and pending["latest_completed"] is None
    assert pending["eligible"] == 2 and pending["outstanding"] is None and pending["unassigned"] is None
    assert pending["accepted"] is None and pending["completion_rate"] is None and pending["completed_runs"] == 0
    k = kpis(body)
    assert (k["accounts"], k["reconciled"], k["not_reconciled"], k["outstanding"]) == (2, 1, 1, 5)
    assert k["completion"] == "40.00"                       # 6 accepted / (13 + 2 eligible): the unreconciled account honestly lowers it
    assert done["account"].startswith("*") and "7766554433" not in str(body) and "9900000111" not in str(body)   # masked everywhere


async def test_entity_and_group_dashboards(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "db", "9900000121")
    await _add_unreconciled_account(db_session, w)
    entity = (await report(client, w["headers"], "entity-dashboard")).json()
    k = kpis(entity)
    assert (k["accounts"], k["reconciled"], k["not_reconciled"], k["eligible"], k["accepted"], k["outstanding"], k["unassigned"], k["completion"]) == (2, 1, 1, 15, 6, 5, 5, "40.00")
    assert entity["level"] == "ENTITY"
    [row] = table_rows(entity, "entities")
    assert row["completion_rate"] == "40.00" and row["amounts"] == "NGN 2,200.00; USD 1,000.00"
    [age] = table_rows(entity, "ageing")
    assert age["band_0-7"] == 5 and age["total"] == 5                                   # all five were opened today
    group = (await report(client, w["headers"], "group-dashboard")).json()
    assert group["level"] == "GROUP" and kpis(group) == k
    [g] = table_rows(group, "groups")
    assert (g["entities"], g["accounts"], g["eligible"], g["accepted"], g["outstanding"], g["completion_rate"]) == (1, 2, 15, 6, 5, "40.00")
    assert any("authorized" in n for n in group["notes"])                                 # states it covers authorized entities only


# ======================================================================================
# the exceptions report content, filters, pagination and validation
# ======================================================================================
async def test_exception_rows_carry_the_required_fields(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "ex", "9900000131")
    h = w["headers"]
    items = await items_by_ref(client, h)
    b5 = _find(items, "BANK_ONLY", "500.00")
    await _act(client, h, b5["id"], "start-investigation")
    await _act(client, h, b5["id"], "comments", {"note": "Called the bank."})
    rows = {r["transaction_reference"]: r for r in table_rows((await report(client, h, "exceptions")).json(), "exceptions")}
    row = rows["ex-B5"]
    today = str(datetime.datetime.now(datetime.UTC).date())
    assert row["reference"].startswith("EXC-") and row["entity"] == w["entity_name"] and row["bank_account"].startswith("*")
    assert (row["transaction_date"], row["amount"], row["currency"], row["category"], row["status"]) == ("2026-06-10", "500.00", "NGN", "BANK_ONLY", "UNDER_REVIEW")
    assert row["assigned_to"] == "Unassigned" and row["opened"] == today and row["last_investigation"] == today
    assert row["age_days"] == 0 and row["has_notes"] == "Yes" and row["resolution_date"] is None
    untouched = rows["ex-B4"]
    assert untouched["last_investigation"] is None and untouched["has_notes"] == "No"
    assert rows["ex-L6"]["category"] == "LEDGER_ONLY"


async def test_exception_filters_pagination_and_ageing_range(client, db_session, demo_group_and_entities, cash_event_types):
    from tests.reconciliation_reports_world import backdate

    w = await build_world(client, db_session, demo_group_and_entities, "fl", "9900000141")
    h = w["headers"]
    items = await items_by_ref(client, h)
    today = datetime.datetime.now(datetime.UTC).date()
    for ref, (cat, amt, cur), age in (("b4", ("BANK_ONLY", "400.00", "NGN"), 3), ("l7", ("LEDGER_ONLY", "700.00", "NGN"), 40)):
        await backdate(db_session, _find(items, cat, amt, cur)["id"], datetime.datetime.combine(today - datetime.timedelta(days=age), datetime.time(9), tzinfo=datetime.UTC))

    async def listed(**params):
        return table_rows((await report(client, h, "exceptions", **params)).json(), "exceptions")

    assert len(await listed()) == 5
    assert {r["category"] for r in await listed(category=["LEDGER_ONLY"])} == {"LEDGER_ONLY"} and len(await listed(category=["LEDGER_ONLY"])) == 2
    assert [r["currency"] for r in await listed(currency_code="USD")] == ["USD"]
    assert len(await listed(txn_date_from="2026-06-11")) == 0 and len(await listed(txn_date_to="2026-06-09")) == 0
    assert len(await listed(txn_date_from="2026-06-10", txn_date_to="2026-06-10")) == 5
    old = await listed(min_age_days=31)
    assert [(r["transaction_reference"], r["age_days"]) for r in old] == [("fl-L7", 40)]
    young = await listed(max_age_days=5)
    assert {r["transaction_reference"] for r in young} == {"fl-B4", "fl-B5", "fl-B8", "fl-L6"}
    assert len(await listed(min_age_days=2, max_age_days=10)) == 1
    assert len(await listed(unassigned="true")) == 5 and len(await listed(exception_status=["RESOLVED"])) == 0
    # pagination is server-side and complete
    page1 = (await report(client, h, "exceptions", page_size=2, page=1)).json()["tables"][0]
    page3 = (await report(client, h, "exceptions", page_size=2, page=3)).json()["tables"][0]
    assert (page1["total_rows"], len(page1["rows"]), len(page3["rows"]), page1["page"], page1["page_size"]) == (5, 2, 1, 1, 2)
    seen = []
    for p in (1, 2, 3):
        seen += [r["reference"] for r in (await report(client, h, "exceptions", page_size=2, page=p)).json()["tables"][0]["rows"]]
    assert len(seen) == 5 and len(set(seen)) == 5


@pytest.mark.parametrize("params, fragment", [
    ({"date_from": "2026-07-01", "date_to": "2026-06-01"}, "date_from must not be after"),
    ({"date_from": "2025-01-01", "date_to": "2026-06-30"}, "may not exceed"),
    ({"min_age_days": 10, "max_age_days": 5}, "min_age_days must not exceed"),
    ({"txn_date_from": "2026-07-01", "txn_date_to": "2026-06-01"}, "txn_date_from must not be after"),
    ({"as_of": "2999-01-01"}, "as_of must not be in the future"),
    ({"unassigned": "true", "assigned_to_user_id": "00000000-0000-0000-0000-000000000001"}, "cannot be combined"),
    ({"currency_code": "NG"}, None), ({"currency_code": "N1G"}, "3-letter"), ({"page_size": 500}, None), ({"page": 0}, None),
    ({"category": ["NOT_A_CATEGORY"]}, None), ({"exception_status": ["BOGUS"]}, None), ({"legal_entity_id": "not-a-uuid"}, None),
])
async def test_invalid_filters_are_rejected_with_422(client, db_session, demo_group_and_entities, cash_event_types, params, fragment):
    w = await build_world(client, db_session, demo_group_and_entities, "vl", "9900000151")
    resp = await client.get("/api/v1/reconciliation/reports/exceptions", params=params, headers=w["headers"])
    assert resp.status_code == 422, resp.text
    if fragment:
        assert fragment in str(resp.json()["detail"])


async def test_unknown_report_is_404_and_catalog_lists_the_reports(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "cat", "9900000161")
    assert (await client.get("/api/v1/reconciliation/reports/not-a-report", headers=w["headers"])).status_code == 404
    catalog = (await client.get("/api/v1/reconciliation/reports", headers=w["headers"])).json()
    assert [c["key"] for c in catalog][:2] == ["overview", "run-summary"] and sum(c["exportable"] for c in catalog) == 8
