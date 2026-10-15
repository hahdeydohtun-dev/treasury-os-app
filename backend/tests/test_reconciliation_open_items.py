import asyncio
import datetime
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_data_model import _login, _make_scoped_user
from tests.test_reconciliation_matching_engine import (
    _create_ready_run,
    _make_bank_statement_txn,
    _make_treasury_txn,
    _set_configuration,
)

BASE = "/api/v1/reconciliation"
LONG = "Investigated with the bank and confirmed the difference is a timing issue."


async def _setup(client, db_session, demo_group_and_entities, label, account_number, entity_index=0):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    entity = (entity_a, entity_b)[entity_index]
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity.id, bank.id, account_number=account_number)
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id, label=label,
    )
    await db_session.flush()
    user.test_id = user.id  # plain attribute: survives commit() expiring the ORM state
    return entity, account, user


async def _headers(client, user):
    return {"Authorization": f"Bearer {await _login(client, user)}"}


async def _execute(client, headers, entity, account):
    run_id = await _create_ready_run(client, headers, entity.id, account.id)
    resp = await client.post(f"{BASE}/runs/{run_id}/execute", headers=headers)
    assert resp.status_code == 200, resp.text
    return run_id


async def _items(client, headers, **params):
    resp = await client.get(f"{BASE}/open-items", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _one_item(client, headers, category=None, **params):
    items = await _items(client, headers, **params)
    if category:
        items = [i for i in items if i["category"] == category]
    assert len(items) == 1, items
    return items[0]


async def _post(client, headers, item_id, action, body=None):
    return await client.post(f"{BASE}/open-items/{item_id}/{action}", json=body or {}, headers=headers)


async def _limited_user(db_session, entity_id, actions, label):
    from app.core.security import hash_password
    from app.models.rbac import (
        EntityScopeType,
        Permission,
        Role,
        TreasuryModule,
        User,
        UserRoleAssignment,
    )

    role = Role(name=f"{label}-{uuid.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()
    for action in actions:
        db_session.add(Permission(role_id=role.id, module=TreasuryModule.BANK_RECONCILIATION, action=action))
    user = User(email=f"{label}-{uuid.uuid4().hex[:8]}@treasuryos.example.com", full_name=label,
                hashed_password=hash_password("Password123!"), is_superuser=False)
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=EntityScopeType.ENTITY,
                                      legal_entity_id=entity_id))
    await db_session.flush()
    return user


# ======================================================================================
# Open-item generation
# ======================================================================================

async def test_unmatched_bank_transaction_creates_bank_only_item(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-bank", "7100000011")
    bank_txn = await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id,
                                              amount="7500000", bank_reference="MISC-1")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)

    item = await _one_item(client, headers)
    assert item["category"] == "BANK_ONLY" and item["status"] == "OPEN"
    assert item["bank_statement_transaction_id"] == str(bank_txn.id) and item["treasury_transaction_id"] is None
    assert item["reconciliation_run_id"] == run_id and item["amount"] == "7500000.00"
    assert item["legal_entity_id"] == str(entity.id) and item["bank_account_id"] == str(account.id)


async def test_unmatched_ledger_transaction_creates_ledger_only_item(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-ledger", "7100000022")
    ledger = await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="3000000",
                                      direction="OUTFLOW", reference="LED-1")
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)

    item = await _one_item(client, headers)
    assert item["category"] == "LEDGER_ONLY" and item["treasury_transaction_id"] == str(ledger.id)
    assert item["bank_statement_transaction_id"] is None


async def test_valid_auto_match_creates_no_open_item(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-match", "7100000033")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, bank_reference="PAY-1")
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, direction="INFLOW", reference="PAY-1")
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)
    assert await _items(client, headers, status=["OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", "REJECTED"]) == []


async def test_ambiguous_one_to_one_tie_is_actionable_and_ledger_rows_are_not_double_counted(
    client, db_session, demo_group_and_entities, cash_event_types,
):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-amb", "7100000044")
    bank_txn = await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, bank_reference="ABC")
    for _ in range(2):
        await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, direction="INFLOW", reference="ABC")
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)

    items = await _items(client, headers)
    assert [i["category"] for i in items] == ["AMBIGUOUS_MATCH"]  # one item; the 2 tied ledger rows add none
    assert items[0]["bank_statement_transaction_id"] == str(bank_txn.id)
    detail = (await client.get(f"{BASE}/open-items/{items[0]['id']}", headers=headers)).json()
    assert len(detail["suggestions"]) == 2 and all(s["status"] == "PENDING" for s in detail["suggestions"])


async def test_review_tier_amount_variance_item_carries_the_variance(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-var", "7100000055")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="1000000")
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="999500", direction="INFLOW")
    await db_session.commit()
    headers = await _headers(client, user)
    await _set_configuration(client, headers, entity.id, amount_tolerance_pct="0.1")
    await _execute(client, headers, entity, account)
    item = await _one_item(client, headers)
    assert item["category"] == "AMOUNT_VARIANCE" and item["amount"] == "500.00"
    assert item["bank_statement_transaction_id"] and item["treasury_transaction_id"]


async def test_review_tier_timing_difference_item(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-time", "7100000066")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, txn_date="2026-06-20")
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, direction="INFLOW",
                             event_date="2026-06-18")
    await db_session.commit()
    headers = await _headers(client, user)
    await _set_configuration(client, headers, entity.id, date_tolerance_days=3)
    await _execute(client, headers, entity, account)
    item = await _one_item(client, headers)
    assert item["category"] == "TIMING_DIFFERENCE" and "2 day" in item["description"]


async def test_review_tier_weak_evidence_is_an_exception_item(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-exc", "7100000077")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id)
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, direction="INFLOW")
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)
    assert (await _one_item(client, headers))["category"] == "EXCEPTION"  # amount+date only: 65 < 70


async def test_valid_advanced_group_creates_no_item_but_ambiguous_group_creates_exactly_one(
    client, db_session, demo_group_and_entities, cash_event_types,
):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-grp", "7100000088")
    # Valid one-to-many group.
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="10000000",
                                   bank_reference="PAYGROUP-OK", txn_date="2026-06-10")
    for amount in ("4000000", "6000000"):
        await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount=amount,
                                 direction="INFLOW", reference="PAYGROUP-OK", event_date="2026-06-10")
    # Ambiguous one-to-many group (two equally valid subsets).
    amb_bank = await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="10000000",
                                              bank_reference="PAYGROUP-AMB", txn_date="2026-06-20", row_number=3)
    for amount in ("9500000", "500000", "500000"):
        await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount=amount,
                                 direction="INFLOW", reference="PAYGROUP-AMB", event_date="2026-06-20")
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)

    items = await _items(client, headers)
    assert len(items) == 1  # nothing for the valid group's 1+2 rows, nothing for the ambiguous group's 3 ledger rows
    assert items[0]["category"] == "ONE_TO_MANY" and items[0]["bank_statement_transaction_id"] == str(amb_bank.id)
    detail = (await client.get(f"{BASE}/open-items/{items[0]['id']}", headers=headers)).json()
    assert len(detail["match_groups"]) == 2 and all(g["ambiguous"] for g in detail["match_groups"])


async def test_reversed_bank_transaction_is_excluded(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.bank_statement import BankStatementTransactionStatus

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-rev", "7100000099")
    txn = await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, bank_reference="REV")
    txn.status = BankStatementTransactionStatus.REVERSED
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)
    assert await _items(client, headers, status=["OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", "REJECTED"]) == []


async def test_repeated_sync_does_not_duplicate_items_events_or_audit(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.audit import AuditEvent
    from app.models.reconciliation import ReconciliationOpenItem, ReconciliationOpenItemEvent

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-idem", "7100000100")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, bank_reference="X-1")
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="42000", direction="OUTFLOW")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)

    async def counts():
        return (
            (await db_session.execute(select(func.count()).select_from(ReconciliationOpenItem))).scalar_one(),
            (await db_session.execute(select(func.count()).select_from(ReconciliationOpenItemEvent))).scalar_one(),
            (await db_session.execute(select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "SYNC_OPEN_ITEMS"))).scalar_one(),
        )

    before = await counts()
    assert before[0] == 2
    for _ in range(2):
        resp = await client.post(f"{BASE}/runs/{run_id}/open-items/sync", headers=headers)
        assert resp.status_code == 200 and resp.json()["created"] == 0 and resp.json()["already_existing"] == 2
    assert await counts() == before


async def test_failed_sync_leaves_no_partial_state(db_session, demo_group_and_entities, cash_event_types):
    import datetime as dt

    from app.models.reconciliation import (
        ReconciliationOpenItem,
        ReconciliationRun,
        ReconciliationRunStatus,
    )
    from app.services.reconciliation_open_item_service import sync_open_items_for_run

    _, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="7100000111")
    await _make_bank_statement_txn(db_session, entity_id=entity_a.id, account_id=account.id)
    run = ReconciliationRun(legal_entity_id=entity_a.id, bank_account_id=account.id,
                            period_start=dt.date(2026, 6, 1), period_end=dt.date(2026, 6, 30),
                            status=ReconciliationRunStatus.COMPLETED)
    db_session.add(run)
    await db_session.flush()

    # The audit write happens AFTER the items/events are inserted, inside the savepoint.
    with patch("app.services.reconciliation_open_item_service.record_audit_event",
               new=AsyncMock(side_effect=RuntimeError("boom"))), pytest.raises(RuntimeError):
        await sync_open_items_for_run(db_session, run)
    assert (await db_session.execute(select(func.count()).select_from(ReconciliationOpenItem))).scalar_one() == 0


async def test_item_age_is_computed_from_the_business_date(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "gen-age", "7100000122")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, txn_date="2026-06-15")
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)
    item = await _one_item(client, headers)
    assert item["item_date"] == "2026-06-15"
    assert item["age_days"] == (datetime.date.today() - datetime.date(2026, 6, 15)).days


# ======================================================================================
# Workflow
# ======================================================================================

async def _bank_only_item(client, db_session, demo_group_and_entities, label, account_number):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, label, account_number)
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="500000",
                                   bank_reference=f"WF-{label}")
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)
    return entity, account, user, headers, await _one_item(client, headers)


async def test_authorized_assignment_succeeds_and_records_history(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-assign", "7200000011")
    from app.models.rbac import EntityScopeType

    colleague = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id,
                                        label="colleague")
    colleague.test_id = colleague.id
    await db_session.commit()
    resp = await _post(client, headers, item["id"], "assign", {
        "assignee_user_id": str(colleague.test_id), "reason": "Owns this bank account", "due_date": "2026-12-31",
        "priority": "HIGH"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_to_user_id"] == str(colleague.test_id) and body["assigned_by_user_id"] == str(user.test_id)
    assert body["assigned_at"] and body["due_date"] == "2026-12-31" and body["priority"] == "HIGH"
    history = (await client.get(f"{BASE}/open-items/{item['id']}/history", headers=headers)).json()
    assert [e["event_type"] for e in history] == ["CREATED", "ASSIGNED"]
    assert history[1]["assignee_user_id"] == str(colleague.test_id) and history[1]["note"] == "Owns this bank account"


async def test_reassignment_preserves_assignment_history(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-reassign", "7200000022")
    from app.models.rbac import EntityScopeType

    first = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id, label="first")
    second = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id, label="second")
    first.test_id, second.test_id = first.id, second.id
    await db_session.commit()
    assert (await _post(client, headers, item["id"], "assign", {"assignee_user_id": str(first.test_id), "reason": "initial"})).status_code == 200
    assert (await _post(client, headers, item["id"], "assign", {"assignee_user_id": str(second.test_id), "reason": "on leave"})).status_code == 200
    # Assigning to the same person again is refused.
    assert (await _post(client, headers, item["id"], "assign", {"assignee_user_id": str(second.test_id), "reason": "again"})).status_code == 400
    history = (await client.get(f"{BASE}/open-items/{item['id']}/history", headers=headers)).json()
    assigned = [e for e in history if e["event_type"] in ("ASSIGNED", "REASSIGNED")]
    assert [(e["event_type"], e["assignee_user_id"]) for e in assigned] == [
        ("ASSIGNED", str(first.test_id)), ("REASSIGNED", str(second.test_id))]


async def test_unauthorized_assignment_is_forbidden(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import TreasuryAction

    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-noassign", "7200000033")
    viewer = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW, TreasuryAction.INVESTIGATE], "viewer")
    await db_session.commit()
    resp = await _post(client, await _headers(client, viewer), item["id"], "assign",
                       {"assignee_user_id": str(user.test_id), "reason": "try it"})
    assert resp.status_code == 403


async def test_invalid_inactive_or_unauthorized_assignee_is_rejected(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import EntityScopeType, TreasuryAction

    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-badassignee", "7200000044")
    _, _, entity_b = demo_group_and_entities
    inactive = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id, label="inactive")
    inactive.is_active = False
    other_entity_user = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_b.id, label="elsewhere")
    cannot_investigate = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW], "viewonly")
    bad_ids = [uuid.uuid4(), inactive.id, other_entity_user.id, cannot_investigate.id]
    await db_session.commit()
    for assignee_id in bad_ids:
        resp = await _post(client, headers, item["id"], "assign", {"assignee_user_id": str(assignee_id), "reason": "assign them"})
        assert resp.status_code == 400, (assignee_id, resp.text)


async def test_start_investigation_transition_and_comment_and_evidence(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-invest", "7200000055")
    started = await _post(client, headers, item["id"], "start-investigation")
    assert started.status_code == 200 and started.json()["status"] == "UNDER_REVIEW"
    assert (await _post(client, headers, item["id"], "start-investigation")).status_code == 400  # not OPEN any more

    assert (await _post(client, headers, item["id"], "comments", {"note": "Called the bank; awaiting statement."})).status_code == 200
    assert (await _post(client, headers, item["id"], "evidence", {"reference": "JV-2026-0615", "note": "Journal ref"})).status_code == 200
    for bad in ("https://evil.example/x", "/etc/passwd", "C:\\\\x", "../../secret", "file:///x"):
        resp = await _post(client, headers, item["id"], "evidence", {"reference": bad})
        assert resp.status_code == 422, bad
    history = (await client.get(f"{BASE}/open-items/{item['id']}/history", headers=headers)).json()
    assert [e["event_type"] for e in history] == ["CREATED", "INVESTIGATION_STARTED", "COMMENT", "EVIDENCE"]
    assert history[2]["note"] == "Called the bank; awaiting statement." and history[3]["reference"] == "JV-2026-0615"


async def test_resolution_requires_reason_and_investigation_and_closure_requires_resolved(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-resolve", "7200000066")
    iid = item["id"]
    assert (await _post(client, headers, iid, "resolve", {"reason": LONG})).status_code == 400      # still OPEN
    assert (await _post(client, headers, iid, "close", {})).status_code == 400                         # not RESOLVED
    await _post(client, headers, iid, "start-investigation")
    assert (await _post(client, headers, iid, "resolve", {"reason": "ok"})).status_code == 422        # not meaningful
    assert (await _post(client, headers, iid, "resolve", {"reason": "          "})).status_code == 422
    assert (await _post(client, headers, iid, "close", {})).status_code == 400                         # UNDER_REVIEW
    resolved = await _post(client, headers, iid, "resolve", {"reason": LONG})
    assert resolved.status_code == 200 and resolved.json()["status"] == "RESOLVED"
    assert resolved.json()["resolution_reason"] == LONG and resolved.json()["resolved_by_user_id"] == str(user.test_id)
    closed = await _post(client, headers, iid, "close", {"note": "Confirmed by treasury manager."})
    assert closed.status_code == 200 and closed.json()["status"] == "CLOSED" and closed.json()["closed_at"]

    # Closing never deletes: gone from the default queue, present through an explicit filter.
    assert await _items(client, headers) == []
    assert [i["id"] for i in await _items(client, headers, status=["CLOSED"])] == [iid]
    assert (await _post(client, headers, iid, "assign", {"assignee_user_id": str(user.test_id), "reason": "late"})).status_code == 400  # terminal


async def test_rejection_requires_reason_and_stays_in_history(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-reject", "7200000077")
    assert (await _post(client, headers, item["id"], "reject", {"reason": "dup"})).status_code == 422
    rejected = await _post(client, headers, item["id"], "reject", {"reason": "Duplicate of an item already tracked elsewhere."})
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
    assert rejected.json()["rejection_reason"].startswith("Duplicate") and rejected.json()["rejected_at"]
    assert [i["id"] for i in await _items(client, headers, status=["REJECTED"])] == [item["id"]]
    assert (await _post(client, headers, item["id"], "start-investigation")).status_code == 400


async def test_reopen_requires_reason_and_close_permission_and_keeps_history(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import TreasuryAction

    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-reopen", "7200000088")
    iid = item["id"]
    assert (await _post(client, headers, iid, "reopen", {"reason": "Reopening this one"})).status_code == 400  # OPEN can't reopen
    await _post(client, headers, iid, "start-investigation")
    await _post(client, headers, iid, "resolve", {"reason": LONG})
    await _post(client, headers, iid, "close", {})

    no_close = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW, TreasuryAction.COMMENT], "noclose")
    await db_session.commit()
    assert (await _post(client, await _headers(client, no_close), iid, "reopen",
                        {"reason": "Not allowed to do this"})).status_code == 403
    assert (await _post(client, headers, iid, "reopen", {"reason": "short"})).status_code == 422
    reopened = await _post(client, headers, iid, "reopen", {"reason": "New statement shows the amount was wrong."})
    body = reopened.json()
    assert reopened.status_code == 200 and body["status"] == "OPEN"
    assert body["resolved_at"] is None and body["closed_at"] is None and body["resolution_reason"] is None
    history = (await client.get(f"{BASE}/open-items/{iid}/history", headers=headers)).json()
    assert [e["event_type"] for e in history] == ["CREATED", "INVESTIGATION_STARTED", "RESOLVED", "CLOSED", "REOPENED"]
    assert history[2]["note"] == LONG  # the earlier resolution is still in the history


async def test_concurrent_conflicting_updates_one_wins(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import ReconciliationOpenItemEvent

    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "wf-race", "7200000099")
    await _post(client, headers, item["id"], "start-investigation")
    results = await asyncio.gather(
        _post(client, headers, item["id"], "resolve", {"reason": LONG}),
        _post(client, headers, item["id"], "reject", {"reason": "Rejected by a second reviewer at once."}),
        return_exceptions=True,
    )
    codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert codes == [200, 400]
    finals = (await db_session.execute(select(ReconciliationOpenItemEvent.event_type).where(
        ReconciliationOpenItemEvent.open_item_id == uuid.UUID(item["id"]),
        ReconciliationOpenItemEvent.event_type.in_(("RESOLVED", "REJECTED"))))).scalars().all()
    assert len(finals) == 1  # exactly one disposition was ever recorded


async def test_default_queue_filters_sorting_and_history_access(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "q-filter", "7300000011")
    for amount, date, ref in (("100000", "2026-06-05", "A"), ("900000", "2026-06-10", "B"), ("500000", "2026-06-20", "C")):
        await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount=amount,
                                       txn_date=date, bank_reference=f"Q-{ref}", row_number=ord(ref))
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="12345", direction="OUTFLOW",
                             event_date="2026-06-12", reference="LEDONLY")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)

    assert [i["amount"] for i in await _items(client, headers)] == ["100000.00", "900000.00", "12345.00", "500000.00"]  # oldest first
    assert [i["amount"] for i in await _items(client, headers, sort="amount_desc")][:2] == ["900000.00", "500000.00"]
    assert {i["category"] for i in await _items(client, headers, category="LEDGER_ONLY")} == {"LEDGER_ONLY"}
    assert len(await _items(client, headers, min_amount=400000, max_amount=600000)) == 1
    assert len(await _items(client, headers, run_id=run_id)) == 4
    assert len(await _items(client, headers, currency_code="USD")) == 0
    assert len(await _items(client, headers, unassigned="true")) == 4
    assert len(await _items(client, headers, min_age_days=0, max_age_days=0)) == 0  # all are older than today
    page = await client.get(f"{BASE}/open-items", params={"limit": 2}, headers=headers)
    assert len(page.json()) == 2 and page.headers["X-Total-Count"] == "4"


# ======================================================================================
# Security
# ======================================================================================

async def test_entity_a_user_cannot_see_or_act_on_entity_b_items(client, db_session, demo_group_and_entities, cash_event_types):
    entity_b, account_b, user_b = await _setup(client, db_session, demo_group_and_entities, "sec-b", "7400000011", entity_index=1)
    entity_a, account_a, user_a = await _setup(client, db_session, demo_group_and_entities, "sec-a", "7400000022")
    await _make_bank_statement_txn(db_session, entity_id=entity_b.id, account_id=account_b.id, amount="999000", bank_reference="B-ONLY")
    await _make_bank_statement_txn(db_session, entity_id=entity_a.id, account_id=account_a.id, amount="111000", bank_reference="A-ONLY")
    await db_session.commit()
    headers_b, headers_a = await _headers(client, user_b), await _headers(client, user_a)
    await _execute(client, headers_b, entity_b, account_b)
    await _execute(client, headers_a, entity_a, account_a)
    b_item = await _one_item(client, headers_b)

    assert [i["amount"] for i in await _items(client, headers_a)] == ["111000.00"]      # list excludes B
    assert (await client.get(f"{BASE}/open-items", params={"legal_entity_id": str(entity_b.id)}, headers=headers_a)).status_code == 403
    missing = await client.get(f"{BASE}/open-items/{uuid.uuid4()}", headers=headers_a)
    for method, path, body in (
        ("get", f"open-items/{b_item['id']}", None), ("get", f"open-items/{b_item['id']}/history", None),
        ("post", f"open-items/{b_item['id']}/assign", {"assignee_user_id": str(user_a.test_id), "reason": "grab it"}),
        ("post", f"open-items/{b_item['id']}/start-investigation", {}),
        ("post", f"open-items/{b_item['id']}/comments", {"note": "peek"}),
        ("post", f"open-items/{b_item['id']}/evidence", {"reference": "X-1"}),
        ("post", f"open-items/{b_item['id']}/resolve", {"reason": LONG}),
        ("post", f"open-items/{b_item['id']}/close", {}),
        ("post", f"open-items/{b_item['id']}/reject", {"reason": LONG}),
        ("post", f"open-items/{b_item['id']}/reopen", {"reason": LONG}),
    ):
        resp = await getattr(client, method)(f"{BASE}/{path}", headers=headers_a, **({"json": body} if body is not None else {}))
        # 404, indistinguishable from an item that does not exist: no way to enumerate B's IDs.
        assert resp.status_code == 404 and resp.json() == missing.json(), (method, path)
    # ...and B's item is untouched.
    assert (await _one_item(client, headers_b))["status"] == "OPEN"


async def test_cross_group_access_is_denied(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.entity import Group, LegalEntity
    from app.models.rbac import EntityScopeType

    entity_a, account_a, user_a = await _setup(client, db_session, demo_group_and_entities, "xg-a", "7400000033")
    group2 = Group(name="OI Group 2", code="OIG2", reporting_currency_code="NGN")
    db_session.add(group2)
    await db_session.flush()
    entity_c = LegalEntity(group_id=group2.id, name="OI Entity C", code="OIENTC", functional_currency_code="NGN", country="NG")
    db_session.add(entity_c)
    await db_session.flush()
    user_c = await _make_scoped_user(db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group2.id, label="xg-c")
    await _make_bank_statement_txn(db_session, entity_id=entity_a.id, account_id=account_a.id, amount="321000")
    await db_session.commit()
    await _execute(client, await _headers(client, user_a), entity_a, account_a)
    a_item = await _one_item(client, await _headers(client, user_a))
    headers_c = await _headers(client, user_c)
    assert await _items(client, headers_c) == []
    assert (await client.get(f"{BASE}/open-items/{a_item['id']}", headers=headers_c)).status_code == 404
    assert (await _post(client, headers_c, a_item["id"], "close", {})).status_code == 404


async def test_history_cannot_be_edited_through_the_api(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "sec-immut", "7400000044")
    for method in ("put", "patch", "delete"):
        resp = await getattr(client, method)(f"{BASE}/open-items/{item['id']}/history", headers=headers)
        assert resp.status_code in (404, 405)
        resp = await getattr(client, method)(f"{BASE}/open-items/{item['id']}", headers=headers)
        assert resp.status_code in (404, 405)


# ======================================================================================
# Audit + financial integrity
# ======================================================================================

async def test_every_workflow_action_is_audited_with_actor_and_states(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.audit import AuditEvent
    from app.models.rbac import EntityScopeType

    entity, account, user, headers, item = await _bank_only_item(client, db_session, demo_group_and_entities, "audit", "7500000011")
    colleague = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id, label="aud-col")
    colleague.test_id = colleague.id
    await db_session.commit()
    iid = item["id"]
    entity_id = uuid.UUID(item["legal_entity_id"])
    await _post(client, headers, iid, "assign", {"assignee_user_id": str(colleague.test_id), "reason": "your account"})
    await _post(client, headers, iid, "start-investigation")
    await _post(client, headers, iid, "comments", {"note": "looking into it"})
    await _post(client, headers, iid, "evidence", {"reference": "TICKET-42"})
    await _post(client, headers, iid, "resolve", {"reason": LONG})
    await _post(client, headers, iid, "close", {"note": "done"})
    await _post(client, headers, iid, "reopen", {"reason": "Found a second related payment."})
    await _post(client, headers, iid, "reject", {"reason": "Actually not our transaction at all."})

    events = (await db_session.execute(select(AuditEvent).where(
        AuditEvent.record_type == "ReconciliationOpenItem", AuditEvent.record_id == iid).order_by(AuditEvent.created_at))).scalars().all()
    assert [e.action for e in events] == ["ASSIGNED", "INVESTIGATION_STARTED", "COMMENT_ADDED", "EVIDENCE_ADDED",
                                           "RESOLVED", "CLOSED", "REOPENED", "REJECTED"]
    assert all(e.user_id == user.test_id and e.legal_entity_id == entity_id for e in events)
    resolved = next(e for e in events if e.action == "RESOLVED")
    assert resolved.previous_value["status"] == "UNDER_REVIEW" and resolved.new_value["status"] == "RESOLVED"
    assert resolved.reason == LONG
    assert "password" not in str([e.new_value for e in events]).lower()


async def test_workflow_never_changes_ledger_bank_or_cash_records(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.balance import BankBalance
    from app.models.bank_statement import BankStatementTransaction
    from app.models.rbac import EntityScopeType
    from app.models.treasury_transaction import TreasuryTransaction

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "fin", "7500000022")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="777000", bank_reference="F-1")
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="12000", direction="OUTFLOW")
    db_session.add(BankBalance(bank_account_id=account.id, balance_date=datetime.date(2026, 5, 31), currency_code="NGN",
                               closing_balance=Decimal(5000000), available_balance=Decimal(5000000), source="MANUAL"))
    colleague = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id, label="fin-col")
    colleague.test_id = colleague.id
    await db_session.commit()
    headers = await _headers(client, user)

    async def snapshot():
        db_session.expire_all()
        return (
            (await db_session.execute(select(TreasuryTransaction.id, TreasuryTransaction.transaction_amount,
                                             TreasuryTransaction.status, TreasuryTransaction.direction)
                                      .order_by(TreasuryTransaction.id))).all(),
            (await db_session.execute(select(BankStatementTransaction.id, BankStatementTransaction.amount,
                                             BankStatementTransaction.status).order_by(BankStatementTransaction.id))).all(),
            (await db_session.execute(select(BankBalance.id, BankBalance.closing_balance, BankBalance.available_balance))).all(),
        )

    await _execute(client, headers, entity, account)
    before = await snapshot()
    for item in await _items(client, headers):
        iid = item["id"]
        await _post(client, headers, iid, "assign", {"assignee_user_id": str(colleague.test_id), "reason": "please check"})
        await _post(client, headers, iid, "start-investigation")
        await _post(client, headers, iid, "comments", {"note": "note"})
        await _post(client, headers, iid, "evidence", {"reference": "REF-1"})
        await _post(client, headers, iid, "resolve", {"reason": LONG})
        await _post(client, headers, iid, "close", {})
        await _post(client, headers, iid, "reopen", {"reason": "Reopened to test no side effects."})
        await _post(client, headers, iid, "reject", {"reason": "Rejected to test no side effects."})
    assert await snapshot() == before  # not one ledger/statement/balance value or row count changed


async def test_sync_endpoint_requires_a_completed_run_and_execute_permission(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import TreasuryAction

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "sync-guard", "7600000011")
    limited = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW], "sync-viewer")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _create_ready_run(client, headers, entity.id, account.id)          # READY, not COMPLETED
    not_done = await client.post(f"{BASE}/runs/{run_id}/open-items/sync", headers=headers)
    assert not_done.status_code == 400
    await client.post(f"{BASE}/runs/{run_id}/execute", headers=headers)
    assert (await client.post(f"{BASE}/runs/{run_id}/open-items/sync", headers=await _headers(client, limited))).status_code == 403
    assert (await client.post(f"{BASE}/runs/{uuid.uuid4()}/open-items/sync", headers=headers)).status_code == 404


async def test_assignable_users_lists_only_active_authorized_users(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import EntityScopeType, TreasuryAction

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "assignable", "7600000022")
    _, _, entity_b = demo_group_and_entities
    ok_user = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id, label="ok-assignee")
    inactive = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity.id, label="inactive-assignee")
    inactive.is_active = False
    elsewhere = await _make_scoped_user(db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_b.id, label="other-entity")
    view_only = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW], "view-only-assignee")
    can_assign_not = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW], "no-assign")
    ids = {"ok": ok_user.id, "inactive": inactive.id, "elsewhere": elsewhere.id, "view_only": view_only.id}
    await db_session.commit()
    resp = await client.get(f"{BASE}/open-items/assignable-users", params={"legal_entity_id": str(entity.id)},
                            headers=await _headers(client, user))
    assert resp.status_code == 200
    listed = {u["id"] for u in resp.json()}
    assert str(ids["ok"]) in listed
    assert not ({str(ids["inactive"]), str(ids["elsewhere"]), str(ids["view_only"])} & listed)
    forbidden = await client.get(f"{BASE}/open-items/assignable-users", params={"legal_entity_id": str(entity.id)},
                                 headers=await _headers(client, can_assign_not))
    assert forbidden.status_code == 403


async def test_high_value_item_is_high_priority(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "prio", "7600000033")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="5000000", bank_reference="BIG")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="1000", bank_reference="SMALL", row_number=3)
    await db_session.commit()
    headers = await _headers(client, user)
    await _set_configuration(client, headers, entity.id, high_value_threshold="1000000")
    await _execute(client, headers, entity, account)
    by_amount = {i["amount"]: i["priority"] for i in await _items(client, headers)}
    assert by_amount == {"5000000.00": "HIGH", "1000.00": "MEDIUM"}
    assert next(iter([i["amount"] for i in await _items(client, headers, sort="priority")])) == "5000000.00"
