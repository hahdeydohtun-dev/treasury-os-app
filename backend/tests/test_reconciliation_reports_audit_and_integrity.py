"""Stage 5F: audit traceability of report generation/export, and proof that reporting never changes any record."""
import json

from sqlalchemy import func, select

from tests.reconciliation_reports_world import build_world, export, report
from tests.test_reconciliation_open_items import _headers, _limited_user

EXPORTABLE = ["run-summary", "matching-performance", "exceptions", "ageing", "assignment", "bank-accounts", "entity-dashboard", "group-dashboard"]
ALL = ["overview"] + EXPORTABLE


async def _audits(db_session, action=None):
    from app.models.audit import AuditEvent

    stmt = select(AuditEvent).where(AuditEvent.record_type == "ReconciliationReport").order_by(AuditEvent.created_at, AuditEvent.id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    db_session.expire_all()
    return list((await db_session.execute(stmt)).scalars().all())


async def test_report_generation_is_audited_with_user_scope_and_filters_but_no_transaction_data(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "au1", "9930000011")
    assert (await _audits(db_session)) == []
    resp = await report(client, w["headers"], "exceptions", category=["LEDGER_ONLY"], currency_code="NGN")
    assert resp.status_code == 200
    [event] = await _audits(db_session, "REPORT_GENERATED")
    assert event.module == "BANK_RECONCILIATION" and event.record_id == "exceptions" and event.user_id == w["user_id"]
    value = event.new_value
    assert value["report"] == "exceptions" and value["outcome"] == "SUCCESS" and value["rows"] == 2 and value["format"] is None
    assert value["filters"]["category"] == ["LEDGER_ONLY"] and value["filters"]["currency_code"] == "NGN"
    assert value["entity_scope"] == [str(w["entity"])]                                  # the derived entity scope is recorded
    blob = json.dumps(value)
    assert "au1-" not in blob and "600.00" not in blob and "700.00" not in blob          # no transaction references or amounts


async def test_successful_and_failed_exports_are_audited(client, db_session, demo_group_and_entities, cash_event_types, monkeypatch):
    from app.models.rbac import TreasuryAction
    from app.services import reconciliation_report_service as svc

    w = await build_world(client, db_session, demo_group_and_entities, "au2", "9930000021")
    viewer = await _limited_user(db_session, w["entity"], [TreasuryAction.VIEW], "audit-viewer")
    await db_session.commit()
    vh = await _headers(client, viewer)

    assert (await export(client, w["headers"], "bank-accounts", "pdf")).status_code == 200
    [ok] = await _audits(db_session, "REPORT_EXPORTED")
    assert (ok.new_value["format"], ok.new_value["outcome"], ok.new_value["report"], ok.user_id) == ("pdf", "SUCCESS", "bank-accounts", w["user_id"])

    assert (await export(client, vh, "exceptions", "xlsx")).status_code == 403                  # no EXPORT permission
    monkeypatch.setattr(svc, "MAX_EXPORT_ROWS", 2)
    assert (await export(client, w["headers"], "exceptions", "xlsx")).status_code == 422       # 5 rows > the cap: refused outright, never silently cut short
    failures = await _audits(db_session, "REPORT_EXPORT_FAILED")
    assert [(f.new_value["outcome"], f.new_value["format"], f.reason) for f in failures] == [("FAILURE", "xlsx", "HTTP 403"), ("FAILURE", "xlsx", "HTTP 422")]
    assert failures[0].new_value["entity_scope"] is None                                       # denied before a scope was derived
    assert failures[1].new_value["entity_scope"] == [str(w["entity"])]
    assert len(await _audits(db_session, "REPORT_EXPORTED")) == 1                               # failures did not count as exports


async def test_reporting_does_not_alter_existing_audit_or_workflow_history(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.audit import AuditEvent
    from app.models.reconciliation import ReconciliationOpenItemEvent

    w = await build_world(client, db_session, demo_group_and_entities, "au3", "9930000031")

    async def history():
        db_session.expire_all()
        events = (await db_session.execute(select(ReconciliationOpenItemEvent.id, ReconciliationOpenItemEvent.event_type, ReconciliationOpenItemEvent.created_at).order_by(ReconciliationOpenItemEvent.id))).all()
        audits = (await db_session.execute(select(AuditEvent.id, AuditEvent.action, AuditEvent.created_at).where(AuditEvent.record_type != "ReconciliationReport").order_by(AuditEvent.id))).all()
        return events, audits

    before = await history()
    for name in ALL:
        assert (await report(client, w["headers"], name)).status_code == 200
    assert await history() == before                                                            # historical events untouched
    assert len(await _audits(db_session, "REPORT_GENERATED")) == len(ALL)                       # one record per report generated


# ======================================================================================
# no reporting operation changes any financial or reconciliation record
# ======================================================================================
async def _snapshot(db_session):
    from app.models.balance import BankBalance
    from app.models.bank_statement import BankStatementTransaction
    from app.models.reconciliation import (
        ReconciliationAcceptedClaim,
        ReconciliationMatchDecision,
        ReconciliationMatchGroup,
        ReconciliationMatchGroupMember,
        ReconciliationMatchSuggestion,
        ReconciliationOpenItem,
        ReconciliationOpenItemEvent,
        ReconciliationRun,
    )
    from app.models.treasury_transaction import TreasuryTransaction

    db_session.expire_all()

    async def rows(model, *cols):
        return (await db_session.execute(select(*[getattr(model, c) for c in cols]).order_by(model.id))).all()

    return {
        "ledger": await rows(TreasuryTransaction, "id", "transaction_amount", "status", "transaction_currency_code", "bank_account_id", "updated_at"),
        "statements": await rows(BankStatementTransaction, "id", "amount", "status", "currency_code", "updated_at"),
        "balances": await rows(BankBalance, "id", "closing_balance", "available_balance", "updated_at"),
        "suggestions": await rows(ReconciliationMatchSuggestion, "id", "status", "confidence", "updated_at"),
        "groups": await rows(ReconciliationMatchGroup, "id", "status", "updated_at"),
        "members": await rows(ReconciliationMatchGroupMember, "id", "match_group_id"),
        "decisions": await rows(ReconciliationMatchDecision, "id", "decision", "reason", "updated_at"),
        "claims": await rows(ReconciliationAcceptedClaim, "id", "claim_kind", "decision_id"),
        "items": await rows(ReconciliationOpenItem, "id", "status", "assigned_to_user_id", "priority", "updated_at"),
        "events": await rows(ReconciliationOpenItemEvent, "id", "event_type", "created_at"),
        "runs": await rows(ReconciliationRun, "id", "status", "matched_count", "unmatched_count", "updated_at"),
    }


async def test_no_report_or_export_changes_financial_records_decisions_or_statuses(client, db_session, demo_group_and_entities, cash_event_types):
    w = await build_world(client, db_session, demo_group_and_entities, "ig", "9930000041")
    before = await _snapshot(db_session)
    assert before["claims"] and before["decisions"] and before["items"] and before["groups"]          # the snapshot is not vacuous
    for name in ALL:
        assert (await report(client, w["headers"], name)).status_code == 200
    for name in EXPORTABLE:
        for fmt in ("xlsx", "pdf"):
            assert (await export(client, w["headers"], name, fmt)).status_code == 200
    after = await _snapshot(db_session)
    for key in before:
        assert after[key] == before[key], key                           # every row, status, amount and timestamp is identical
    statuses = {r[1].value for r in after["suggestions"]}
    assert "PENDING" in statuses and "ACCEPTED" in statuses              # the pending proposal was NOT accepted by being reported on


async def test_reports_over_an_empty_database_return_empty_not_errors(client, db_session, demo_group_and_entities, cash_event_types):
    from tests.test_reconciliation_open_items import _setup

    _entity, _account, user = await _setup(client, db_session, demo_group_and_entities, "emp", "9930000051")
    await db_session.commit()
    headers = await _headers(client, user)
    for name in ALL:
        resp = await report(client, headers, name)
        assert resp.status_code == 200, (name, resp.text[:200])
    body = (await report(client, headers, "bank-accounts")).json()
    [row] = body["tables"][0]["rows"]
    assert row["state"] == "NOT YET RECONCILED" and row["outstanding"] is None and row["eligible"] == 0 and row["completion_rate"] is None
    assert (await export(client, headers, "ageing", "pdf")).status_code == 200 and (await export(client, headers, "run-summary", "xlsx")).status_code == 200
    assert (await db_session.execute(select(func.count()).select_from(__import__("app.models.reconciliation", fromlist=["ReconciliationRun"]).ReconciliationRun))).scalar_one() == 0
