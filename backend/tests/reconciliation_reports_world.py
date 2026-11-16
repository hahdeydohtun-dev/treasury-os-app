"""
Scenario builder shared by the Stage 5F report tests (not a test module itself).

`build_world` creates, for ONE entity and ONE NGN account, a COMPLETED run for June 2026 containing exactly the situations
the report rules must get right, then drives the REAL Stage 5E API (accept / reject / sync) so decisions, claims, open items
and audit history are produced by the production code, not faked:

  bank rows (ACTIVE, in period):  B1 B2 B3 B4 B5 and B8 (USD)        excluded: B6 (REVERSED), B7 (July, outside period)
  ledger rows (POSTED, in period): L1 .. L7

  S1 one-to-one   (B1, L1)               ACCEPTED
  G1 one-to-many  (B2 | L2, L3)          ACCEPTED
  T1 internal transfer (L3, L4)          ACCEPTED   <- L3 is ALSO in G1: a transaction in two accepted proposals
  S2 one-to-one   (B3, L5)               PENDING    (awaiting a decision: neither accepted nor an exception)
  S3 one-to-one   (B4, L6)               REJECTED   (its rows return as exceptions)
  B5, L7, B8      no proposal                        (unmatched)

Expected populations: bank eligible 6 = accepted 2 (B1,B2) + proposed 1 (B3) + unmatched 3 (B4,B5,B8);
ledger eligible 7 = accepted 4 (L1,L2,L3,L4) + proposed 1 (L5) + unmatched 2 (L6,L7). Completion = 6/13.
Open items (all OPEN after the rejection triggers a sync): BANK_ONLY B4, B5, B8(USD); LEDGER_ONLY L6, L7.
"""
import datetime
from decimal import Decimal

from sqlalchemy import update

from tests.test_reconciliation_acceptance_conflicts import (
    _accept,
    _bank,
    _group,
    _ledger,
    _run,
    _suggestion,
)
from tests.test_reconciliation_match_decisions import _decide
from tests.test_reconciliation_open_items import BASE, _headers, _setup

JUNE = ("2026-06-01", "2026-06-30")
REJECT_REASON = "Customer confirmed this receipt belongs to a different invoice."


async def build_world(client, db_session, demo, label, number, *, entity_index=0):
    from app.models.bank_statement import BankStatementTransactionStatus

    entity, account, user = await _setup(client, db_session, demo, label, number, entity_index=entity_index)
    run = await _run(db_session, entity, account, *JUNE)
    d = "2026-06-10"
    b = {
        1: await _bank(db_session, entity, account, f"{label}-B1", amount="1000", row=2, txn_date=d),
        2: await _bank(db_session, entity, account, f"{label}-B2", amount="2000", row=3, txn_date=d),
        3: await _bank(db_session, entity, account, f"{label}-B3", amount="300", row=4, txn_date=d),
        4: await _bank(db_session, entity, account, f"{label}-B4", amount="400", row=5, txn_date=d),
        5: await _bank(db_session, entity, account, f"{label}-B5", amount="500", row=6, txn_date=d),
        6: await _bank(db_session, entity, account, f"{label}-B6", amount="600", row=7, txn_date=d),
        7: await _bank(db_session, entity, account, f"{label}-B7", amount="700", row=8, txn_date="2026-07-05"),
        8: await _bank(db_session, entity, account, f"{label}-B8", amount="1000", row=9, txn_date=d, currency="USD"),
    }
    b[6].status = BankStatementTransactionStatus.REVERSED
    amounts = {1: "1000", 2: "2000", 3: "300", 4: "400", 5: "500", 6: "600", 7: "700"}
    ledger = {}
    for i in range(1, 8):
        ledger[i] = await _ledger(db_session, entity, account, f"{label}-L{i}", amount=amounts[i] if i != 2 else "1200",
                                  event_date=d)
    ledger[3].transaction_amount = Decimal(800)
    s1 = await _suggestion(db_session, run, b[1], ledger[1])
    g1 = await _group(db_session, run, "ONE_TO_MANY", [b[2]], [ledger[2], ledger[3]])
    t1 = await _group(db_session, run, "INTERNAL_TRANSFER", [], [ledger[3], ledger[4]])
    s2 = await _suggestion(db_session, run, b[3], ledger[5])
    s3 = await _suggestion(db_session, run, b[4], ledger[6])
    ids = {"entity": entity.id, "account": account.id, "run": run.id, "s1": s1.id, "g1": g1.id, "t1": t1.id, "s2": s2.id, "s3": s3.id,
           "bank": {k: v.id for k, v in b.items()}, "ledger": {k: v.id for k, v in ledger.items()}, "user_id": user.test_id,
           "entity_name": entity.name, "entity_code": entity.code}
    await db_session.commit()
    headers = await _headers(client, user)
    for kind, pid in (("SUGGESTION", ids["s1"]), ("GROUP", ids["g1"]), ("GROUP", ids["t1"])):
        assert (await _accept(client, headers, kind, pid)).status_code == 200
    assert (await _decide(client, headers, "SUGGESTION", ids["s3"], "reject", REJECT_REASON)).status_code == 200   # also syncs items
    assert (await client.post(f"{BASE}/runs/{ids['run']}/open-items/sync", headers=headers)).status_code == 200
    return {**ids, "headers": headers, "user": user}


async def items_by_ref(client, headers, **params):
    resp = await client.get(f"{BASE}/open-items", params={"status": ["OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", "REJECTED"], **params}, headers=headers)
    assert resp.status_code == 200, resp.text
    return {i["id"]: i for i in resp.json()}


async def backdate(db_session, item_id, created_at: datetime.datetime):
    """Test-only: give an item (and its CREATED event) an earlier opening time so ageing bands can be exercised."""
    from app.models.reconciliation import (
        OpenItemEventType,
        ReconciliationOpenItem,
        ReconciliationOpenItemEvent,
    )

    await db_session.execute(update(ReconciliationOpenItem).where(ReconciliationOpenItem.id == item_id).values(created_at=created_at))
    await db_session.execute(update(ReconciliationOpenItemEvent).where(
        ReconciliationOpenItemEvent.open_item_id == item_id, ReconciliationOpenItemEvent.event_type == OpenItemEventType.CREATED).values(created_at=created_at))
    await db_session.commit()


async def report(client, headers, name, **params):
    resp = await client.get(f"/api/v1/reconciliation/reports/{name}", params={"date_from": JUNE[0], "date_to": JUNE[1], **params}, headers=headers)
    return resp


def table_rows(result, key):
    return next(t for t in result["tables"] if t["key"] == key)["rows"]


def kpis(result):
    return {k["key"]: k["value"] for k in result["kpis"]}


def pdf_text(data: bytes) -> str:
    """Extract the text drawn in a reportlab PDF (ASCII85 + Flate content streams) without any extra dependency."""
    import base64
    import re
    import zlib

    def decode(body: bytes) -> bytes | None:
        body = body.strip()
        if body.endswith(b"~>"):
            body = body[:-2]
        try:
            return zlib.decompress(base64.a85decode(body))
        except Exception:  # noqa: BLE001 - not a text content stream (fonts, images, ...)
            return None

    out = []
    for content in filter(None, (decode(m.group(1)) for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.DOTALL))):
        for s in re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", content):
            out.append(re.sub(rb"\\(.)", rb"\1", s).decode("latin-1"))
    return "\n".join(out)


def xlsx_workbook(data: bytes):
    import io

    import openpyxl

    return openpyxl.load_workbook(io.BytesIO(data))


def sheet_rows(wb, title_prefix):
    ws = next(w for w in wb.worksheets if w.title.lower().startswith(title_prefix.lower()))
    rows = list(ws.iter_rows(values_only=True))
    return rows[0], rows[1:]


async def export(client, headers, name, fmt="xlsx", **params):
    return await client.get(f"/api/v1/reconciliation/reports/{name}/export", params={"format": fmt, "date_from": JUNE[0], "date_to": JUNE[1], **params}, headers=headers)
