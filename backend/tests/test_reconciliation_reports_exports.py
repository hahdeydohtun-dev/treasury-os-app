"""Stage 5F exports: contents, filters, scope, masking, currency labels and formula-injection defence."""
import json
import re

import pytest

from tests.reconciliation_reports_world import (
    build_world,
    export,
    pdf_text,
    report,
    sheet_rows,
    table_rows,
    xlsx_workbook,
)
from tests.test_reconciliation_data_model import _make_scoped_user
from tests.test_reconciliation_open_items import BASE, _headers

EXPORTABLE = ["run-summary", "matching-performance", "exceptions", "ageing", "assignment", "bank-accounts", "entity-dashboard", "group-dashboard"]
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
@pytest.mark.parametrize("name", EXPORTABLE)
async def test_every_report_exports_in_both_formats(client, db_session, demo_group_and_entities, cash_event_types, name, fmt):
    w = await build_world(client, db_session, demo_group_and_entities, "all", "9920000011")
    resp = await export(client, w["headers"], name, fmt)
    assert resp.status_code == 200, resp.text[:200]
    assert resp.content[:2] == b"PK" if fmt == "xlsx" else resp.content[:5] == b"%PDF-"
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats" if fmt == "xlsx" else "application/pdf")
    disposition = resp.headers["content-disposition"]
    assert f".{fmt}" in disposition and name.replace("-", "_") in disposition and "2026-06-01_2026-06-30" in disposition
    assert resp.headers["cache-control"] == "no-store" and not UUID.search(disposition)


async def test_excel_contains_exactly_the_authorized_records_and_the_required_context(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "xl", "9920000021")
    screen = table_rows((await report(client, w["headers"], "exceptions")).json(), "exceptions")
    wb = xlsx_workbook((await export(client, w["headers"], "exceptions")).content)
    header, rows = sheet_rows(wb, "Exceptions")
    ref_col = header.index("Exception")
    assert sorted(r[ref_col] for r in rows) == sorted(r["reference"] for r in screen) and len(rows) == 5
    by_ref = {r[ref_col]: dict(zip(header, r)) for r in rows}
    s = {r["reference"]: r for r in screen}
    for ref, row in by_ref.items():
        assert str(row["Amount"]).replace(".0", "") .startswith(s[ref]["amount"].split(".")[0])
        assert (row["Currency"], row["Category"], row["Status"], row["Entity"]) == (s[ref]["currency"], s[ref]["category"], s[ref]["status"], s[ref]["entity"])
    assert {r["Currency"] for r in by_ref.values()} == {"NGN", "USD"}                       # currency labels preserved
    info = {str(r[0]): str(r[1]) for r in wb["Report Info"].iter_rows(values_only=True) if r[0]}
    assert info["Level"] == "ENTITY" and info["Reporting period"].startswith("2026-06-01 to 2026-06-30")
    assert info["Generated at (UTC)"] and w["entity_name"] in info["Entities in scope"] and "Timezone" in info
    defs = [r[0] for r in wb["Definitions"].iter_rows(values_only=True) if r[0]]
    assert any("exception is an open item" in d for d in defs)                               # methodology included
    names = [ws.title for ws in wb.worksheets]
    assert names[0] == "Report Info" and "KPIs" in names and names[-1] == "Definitions"
    _, amounts_rows = sheet_rows(wb, "Total amount by")
    totals = {r[0]: float(r[1]) for r in amounts_rows}
    assert totals == {"NGN": 2200.0, "USD": 1000.0}                                            # separate, never combined


async def test_pdf_contains_the_records_context_and_definitions(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "pf", "9920000031")
    screen = table_rows((await report(client, w["headers"], "exceptions")).json(), "exceptions")
    text = pdf_text((await export(client, w["headers"], "exceptions", "pdf")).content)
    for r in screen:
        assert r["reference"] in text and r["transaction_reference"] in text
    for needle in ("Open Exceptions", "ENTITY", "2026-06-01 to 2026-06-30", w["entity_name"], "NGN", "USD", "Methodology and definitions", "generated"):
        assert needle in text, needle
    assert "exception is an open item" in text.replace("\n", " ")
    assert "BANK_ONLY" in text and "LEDGER_ONLY" in text


async def test_group_dashboard_export_is_labelled_group_level_and_entity_dashboard_entity_level(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "lv", "9920000041")
    group = pdf_text((await export(client, w["headers"], "group-dashboard", "pdf")).content)
    entity = pdf_text((await export(client, w["headers"], "entity-dashboard", "pdf")).content)
    assert "GROUP" in group and "Group Reconciliation Dashboard" in group
    assert "ENTITY" in entity and "Entity Reconciliation Dashboard" in entity


async def test_export_applies_the_same_filters_as_the_screen_and_is_not_paginated(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "ft", "9920000051")
    h = w["headers"]
    screen = (await report(client, h, "exceptions", category=["LEDGER_ONLY"], currency_code="NGN")).json()["tables"][0]
    _, rows = sheet_rows(xlsx_workbook((await export(client, h, "exceptions", category=["LEDGER_ONLY"], currency_code="NGN")).content), "Exceptions")
    assert len(rows) == screen["total_rows"] == 2 and all(r[6] == "NGN" for r in rows)
    paged = (await report(client, h, "exceptions", page_size=2)).json()["tables"][0]
    exported = sheet_rows(xlsx_workbook((await export(client, h, "exceptions", page_size=2, page=2)).content), "Exceptions")[1]
    assert len(paged["rows"]) == 2 and len(exported) == paged["total_rows"] == 5      # the screen shows a page; the export has every match
    info = {r[0]: r[1] for r in xlsx_workbook((await export(client, h, "exceptions", category=["LEDGER_ONLY"])).content)["Report Info"].iter_rows(values_only=True) if r[0]}
    assert "LEDGER_ONLY" in str(info["Filter: category"])                                # the applied filters are stated in the export


async def test_bank_account_identifiers_are_masked_and_internal_ids_are_not_exported(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "mk", "9920000061")
    for name in EXPORTABLE:
        xl = xlsx_workbook((await export(client, w["headers"], name)).content)
        blob = json.dumps([[str(c) for c in row] for ws in xl.worksheets for row in ws.iter_rows(values_only=True)])
        text = pdf_text((await export(client, w["headers"], name, "pdf")).content)
        for out in (blob, text):
            assert "9920000061" not in out, name                                            # the raw account number never appears
            assert not UUID.search(out), name                                               # no internal UUIDs
    _, rows = sheet_rows(xlsx_workbook((await export(client, w["headers"], "bank-accounts")).content), "Bank accounts")
    assert any("0061" in str(c) and str(c).startswith("*") for r in rows for c in r)         # masked: asterisks + last four only


async def test_exports_exclude_unauthorized_entities_and_group_users_get_theirs(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import EntityScopeType

    group_id = demo_group_and_entities[0].id
    a = await build_world(client, db_session, demo_group_and_entities, "ua", "9920000071", entity_index=0)
    b = await build_world(client, db_session, demo_group_and_entities, "ub", "9920000081", entity_index=1)
    group_user = await _make_scoped_user(db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group_id, label="gx")
    await db_session.commit()
    gh = await _headers(client, group_user)
    for name in EXPORTABLE:
        mine = await export(client, a["headers"], name)
        blob = json.dumps([[str(c) for c in row] for ws in xlsx_workbook(mine.content).worksheets for row in ws.iter_rows(values_only=True)])
        assert b["entity_name"] not in blob and "ub-" not in blob, name
        assert b["entity_name"] not in pdf_text((await export(client, a["headers"], name, "pdf")).content), name
    denied = await export(client, a["headers"], "exceptions", legal_entity_id=str(b["entity"]))
    assert denied.status_code == 403
    both = json.dumps([[str(c) for c in row] for ws in xlsx_workbook((await export(client, gh, "exceptions")).content).worksheets for row in ws.iter_rows(values_only=True)])
    assert a["entity_name"] in both and b["entity_name"] in both


# ======================================================================================
# formula injection
# ======================================================================================
@pytest.mark.parametrize("value, expected", [
    ("=1+1", "'=1+1"), ("+1", "'+1"), ("-1+2", "'-1+2"), ("@SUM(A1)", "'@SUM(A1)"), ("\t=1", "'\t=1"), ("  =1", "'  =1"),
    ('=HYPERLINK("http://evil","x")', "'=HYPERLINK(\"http://evil\",\"x\")"), ("normal text", "normal text"), ("EXC-ABCDEF12", "EXC-ABCDEF12"), ("", ""), (5, 5), (None, None),
])
def test_neutralize_unit(value, expected):
    from app.services.reconciliation_report_export import neutralize
    assert neutralize(value) == expected


async def test_formula_injection_is_neutralized_in_exported_text_fields(client, db_session, demo_group_and_entities, cash_event_types):
    from sqlalchemy import update

    from app.models.rbac import User
    from tests.reconciliation_reports_world import items_by_ref
    from tests.test_reconciliation_matching_engine import _make_bank_statement_txn

    w = await build_world(client, db_session, demo_group_and_entities, "fi", "9920000091")
    evil = '=HYPERLINK("http://evil.example/x","click")'
    await _make_bank_statement_txn(db_session, entity_id=w["entity"], account_id=w["account"], amount="55", bank_reference=evil, txn_date="2026-06-11", row_number=40)
    colleague = await _make_scoped_user(db_session, scope_type=__import__("app.models.rbac", fromlist=["EntityScopeType"]).EntityScopeType.ENTITY, legal_entity_id=w["entity"], label="evilname")
    colleague_id = colleague.id
    await db_session.execute(update(User).where(User.id == colleague_id).values(full_name="@SUM(1+1)*cmd|' /C calc'!A0"))
    await db_session.commit()
    assert (await client.post(f"{BASE}/runs/{w['run']}/open-items/sync", headers=w["headers"])).status_code == 200
    items = await items_by_ref(client, w["headers"])
    target = next(i for i in items.values() if i["amount"] == "55.00")
    assert (await client.post(f"{BASE}/open-items/{target['id']}/assign", json={"assignee_user_id": str(colleague_id), "reason": "please check"}, headers=w["headers"])).status_code == 200

    raw_screen = json.dumps((await report(client, w["headers"], "exceptions")).json())
    assert evil.replace('"', '\\"') in raw_screen                                         # the API returns the data unchanged...
    for name in ("exceptions", "assignment"):
        wb = xlsx_workbook((await export(client, w["headers"], name)).content)
        seen = []
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    assert cell.data_type != "f", (name, ws.title, cell.coordinate, cell.value)   # ...but no cell is ever a formula
                    if isinstance(cell.value, str) and cell.value.lstrip("'").startswith(("=", "@")):
                        seen.append(cell.value)
        if name == "exceptions":
            assert "'" + evil in seen                                                      # neutralized with a leading apostrophe
            assert any(v.startswith("'@SUM") for v in seen)
        assert all(v.startswith("'") for v in seen), seen
