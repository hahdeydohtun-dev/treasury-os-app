"""
Stage 5E match-disposition integrity: PENDING proposal != ACCEPTED reconciliation != unresolved exception.
"""
import asyncio
import datetime
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from tests.test_reconciliation_data_model import _make_scoped_user
from tests.test_reconciliation_matching_engine import (
    _make_bank_statement_txn,
    _make_treasury_txn,
)
from tests.test_reconciliation_open_items import (
    BASE,
    LONG,
    _execute,
    _headers,
    _items,
    _limited_user,
    _post,
    _setup,
)

MP = f"{BASE}/match-proposals"
REJECT_REASON = "Reference matches but the customer confirmed this was a different invoice."


async def _proposals(client, headers, **params):
    resp = await client.get(MP, params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _decide(client, headers, kind, pid, action, reason=None):
    return await client.post(f"{MP}/{kind}/{pid}/{action}", json={"reason": reason} if reason is not None else {}, headers=headers)


async def _exact_match_scenario(client, db_session, demo_group_and_entities, label, number, **kw):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, label, number)
    bank = await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id,
                                          amount="2500000", bank_reference=f"INV-{label}")
    ledger = await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="2500000",
                                      direction="INFLOW", reference=f"INV-{label}")
    ids = (bank.id, ledger.id)
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)
    return entity, account, user, headers, run_id, ids


async def _count(db_session, model, *where):
    return (await db_session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


# ======================================================================================
# 1-2: PENDING is never silently ACCEPTED
# ======================================================================================

async def test_high_confidence_pending_suggestion_is_not_silently_accepted(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import (
        MatchSuggestionStatus,
        ReconciliationMatchDecision,
        ReconciliationMatchSuggestion,
    )

    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "pend1", "8100000011")
    suggestion = (await db_session.execute(select(ReconciliationMatchSuggestion).where(
        ReconciliationMatchSuggestion.reconciliation_run_id == uuid.UUID(run_id),
        ReconciliationMatchSuggestion.treasury_transaction_id.is_not(None)))).scalar_one()
    # Confidence 95 is well above the 70 auto-match threshold, yet the proposal is still only PENDING.
    assert suggestion.confidence == Decimal(95) and suggestion.status == MatchSuggestionStatus.PENDING
    assert await _count(db_session, ReconciliationMatchDecision) == 0
    pending = await _proposals(client, headers)
    assert [(p["kind"], p["status"], p["label"]) for p in pending] == [("SUGGESTION", "PENDING", "ONE_TO_ONE")]
    assert pending[0]["decision"] is None
    summary = (await client.get(f"{MP}/summary", headers=headers)).json()
    assert summary["pending_decisions"] == 1 and summary["open_exceptions"] == 0
    assert await _items(client, headers) == []  # a pending proposal is not an exception...
    assert pending  # ...and it is not hidden: it is waiting in the review queue


async def test_pending_group_is_not_silently_accepted(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import MatchGroupStatus, ReconciliationMatchGroup

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "pend2", "8100000022")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="10000000",
                                   bank_reference="PAYGROUP-1", txn_date="2026-06-10")
    for amount in ("4000000", "6000000"):
        await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount=amount,
                                 direction="INFLOW", reference="PAYGROUP-1", event_date="2026-06-10")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)
    group = (await db_session.execute(select(ReconciliationMatchGroup).where(
        ReconciliationMatchGroup.reconciliation_run_id == uuid.UUID(run_id)))).scalar_one()
    assert group.status == MatchGroupStatus.PENDING
    proposals = await _proposals(client, headers, kind="GROUP")
    assert [(p["status"], p["label"], p["member_count"]) for p in proposals] == [("PENDING", "ONE_TO_MANY", 3)]
    assert await _items(client, headers) == []


# ======================================================================================
# 3-6: explicit acceptance / rejection
# ======================================================================================

async def test_explicit_acceptance_records_decision_status_and_audit(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.audit import AuditEvent
    from app.models.reconciliation import (
        MatchSuggestionStatus,
        ReconciliationMatchDecision,
        ReconciliationMatchSuggestion,
    )

    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "acc", "8100000033")
    listed = (await _proposals(client, headers))[0]
    pid, entity_id = listed["id"], listed["legal_entity_id"]
    resp = await _decide(client, headers, "SUGGESTION", pid, "accept", "Verified against the remittance advice.")
    assert resp.status_code == 200 and resp.json()["status"] == "ACCEPTED" and resp.json()["already_decided"] is False

    db_session.expire_all()
    suggestion = await db_session.get(ReconciliationMatchSuggestion, uuid.UUID(pid))
    assert suggestion.status == MatchSuggestionStatus.ACCEPTED
    record = (await db_session.execute(select(ReconciliationMatchDecision))).scalar_one()
    assert (record.previous_status, record.new_status, record.reason) == ("PENDING", "ACCEPTED", "Verified against the remittance advice.")
    assert record.decided_by_user_id == user.test_id and str(record.reconciliation_run_id) == run_id
    audit = (await db_session.execute(select(AuditEvent).where(AuditEvent.action == "MATCH_ACCEPTED"))).scalar_one()
    assert audit.record_id == pid and audit.user_id == user.test_id and str(audit.legal_entity_id) == entity_id
    assert audit.previous_value["status"] == "PENDING" and audit.new_value["status"] == "ACCEPTED"
    assert audit.new_value["reconciliation_run_id"] == run_id and audit.reason == "Verified against the remittance advice."
    accepted = await _proposals(client, headers, status="ACCEPTED")
    assert accepted[0]["decision"]["decision"] == "ACCEPTED" and (await client.get(f"{MP}/summary", headers=headers)).json()["pending_decisions"] == 0


async def test_rejection_requires_a_meaningful_reason(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "rej0", "8100000044")
    pid = (await _proposals(client, headers))[0]["id"]
    for reason in (None, "", "   ", "nope"):
        assert (await _decide(client, headers, "SUGGESTION", pid, "reject", reason)).status_code == 422
    assert (await _proposals(client, headers))[0]["status"] == "PENDING"  # nothing changed


async def test_accepted_match_creates_no_ordinary_exception_even_after_resync(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "acc2", "8100000055")
    pid = (await _proposals(client, headers))[0]["id"]
    await _decide(client, headers, "SUGGESTION", pid, "accept")
    for _ in range(2):
        assert (await client.post(f"{BASE}/runs/{run_id}/open-items/sync", headers=headers)).json()["created"] == 0
    assert await _items(client, headers, status=["OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", "REJECTED"]) == []


async def test_rejected_proposal_becomes_actionable_exceptions_with_its_reason(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers, run_id, (bank_id, ledger_id) = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "rej1", "8100000066")
    pid = (await _proposals(client, headers))[0]["id"]
    resp = await _decide(client, headers, "SUGGESTION", pid, "reject", REJECT_REASON)
    assert resp.status_code == 200 and resp.json()["status"] == "REJECTED"

    items = await _items(client, headers)
    assert sorted(i["category"] for i in items) == ["BANK_ONLY", "LEDGER_ONLY"]
    assert {i["bank_statement_transaction_id"] for i in items} == {str(bank_id), None}
    assert all(REJECT_REASON in i["description"] and i["reason"] == "Proposed match rejected" for i in items)
    rejected = await _proposals(client, headers, status="REJECTED")  # the disposition stays visible
    assert rejected[0]["decision"]["reason"] == REJECT_REASON and rejected[0]["status"] == "REJECTED"
    assert (await client.get(f"{MP}/summary", headers=headers)).json() == {
        "pending_decisions": 0, "pending_suggestions": 0, "pending_groups": 0, "open_exceptions": 2}
    # re-synchronizing neither duplicates the new items nor loses them
    assert (await client.post(f"{BASE}/runs/{run_id}/open-items/sync", headers=headers)).json()["created"] == 0
    assert len(await _items(client, headers)) == 2


# ======================================================================================
# 7: ambiguous / weak proposals stay in the exception queue and are not decidable
# ======================================================================================

async def test_ambiguous_and_review_tier_proposals_stay_reviewable_as_exceptions(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import MatchSuggestionStatus, ReconciliationMatchSuggestion

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "amb", "8100000077")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, bank_reference="TIE", txn_date="2026-06-10")
    for _ in range(2):
        await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, direction="INFLOW",
                                 reference="TIE", event_date="2026-06-10")
    # a weak (review-tier) pair: amount + date only
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="777000", row_number=3, txn_date="2026-06-15")
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="777000", direction="INFLOW", event_date="2026-06-15")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)

    assert sorted(i["category"] for i in await _items(client, headers)) == ["AMBIGUOUS_MATCH", "EXCEPTION"]
    assert await _proposals(client, headers) == []  # neither is an approvable proposal
    suggestions = (await db_session.execute(select(ReconciliationMatchSuggestion).where(
        ReconciliationMatchSuggestion.reconciliation_run_id == uuid.UUID(run_id),
        ReconciliationMatchSuggestion.treasury_transaction_id.is_not(None)))).scalars().all()
    assert len(suggestions) == 3 and all(s.status == MatchSuggestionStatus.PENDING for s in suggestions)
    for s in suggestions:  # each is refused with an explanation pointing at the investigation queue
        resp = await _decide(client, headers, "SUGGESTION", s.id, "accept")
        assert resp.status_code == 400 and "open-items queue" in resp.json()["detail"]
        assert (await client.get(f"{MP}/SUGGESTION/{s.id}", headers=headers)).json()["not_decidable_reason"]


async def test_ambiguous_group_is_an_exception_not_a_decidable_proposal(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import ReconciliationMatchGroup

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "ambgrp", "8100000088")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="10000000",
                                   bank_reference="PAYGROUP-AMB", txn_date="2026-06-20")
    for amount in ("9500000", "500000", "500000"):
        await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount=amount,
                                 direction="INFLOW", reference="PAYGROUP-AMB", event_date="2026-06-20")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)
    groups = (await db_session.execute(select(ReconciliationMatchGroup).where(
        ReconciliationMatchGroup.reconciliation_run_id == uuid.UUID(run_id)))).scalars().all()
    assert len(groups) == 2 and all(g.status.value == "AMBIGUOUS" for g in groups)
    assert await _proposals(client, headers) == [] and [i["category"] for i in await _items(client, headers)] == ["ONE_TO_MANY"]
    resp = await _decide(client, headers, "GROUP", groups[0].id, "accept")
    assert resp.status_code == 400 and "AMBIGUOUS" in resp.json()["detail"]


# ======================================================================================
# 8: every Stage 5D relationship type gets a valid disposition
# ======================================================================================

async def _build_group(db_session, entity, account, relationship):
    from app.models.reconciliation import (
        MatchGroupStatus,
        MatchRelationshipType,
        ReconciliationMatchGroup,
        ReconciliationMatchGroupMember,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    rel = MatchRelationshipType[relationship]
    run = ReconciliationRun(legal_entity_id=entity.id, bank_account_id=account.id, period_start=datetime.date(2026, 6, 1),
                            period_end=datetime.date(2026, 6, 30), status=ReconciliationRunStatus.COMPLETED)
    db_session.add(run)
    await db_session.flush()
    bank_ids, ledger_ids, currency = [], [], "NGN"
    ref = f"GRP-{relationship}"
    if rel in (MatchRelationshipType.ONE_TO_MANY, MatchRelationshipType.BATCH):
        bank_ids = [(await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="10000000", bank_reference=ref)).id]
        ledger_ids = [(await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount=a, direction="INFLOW", reference=ref)).id for a in ("4000000", "6000000")]
        agg = (Decimal(10000000), Decimal(10000000))
    elif rel == MatchRelationshipType.MANY_TO_ONE:
        bank_ids = [(await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount=a, bank_reference=ref, row_number=2 + i)).id for i, a in enumerate(("4000000", "6000000"))]
        ledger_ids = [(await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="10000000", direction="INFLOW", reference=ref)).id]
        agg = (Decimal(10000000), Decimal(10000000))
    elif rel == MatchRelationshipType.INTERNAL_TRANSFER:
        ledger_ids = [(await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="500000", direction=d, reference=ref)).id for d in ("OUTFLOW", "INFLOW")]
        agg = (Decimal(500000), Decimal(500000))
    else:  # FX_MATCH: USD bank amount against an NGN ledger amount
        currency = "NGN"
        bank_ids = [(await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, currency="USD", amount="1000", bank_reference=ref)).id]
        ledger_ids = [(await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, currency="NGN", amount="1600000", direction="INFLOW", reference=ref)).id]
        agg = (Decimal(1000), Decimal(1600000))
    fx = rel == MatchRelationshipType.FX_MATCH
    group = ReconciliationMatchGroup(
        reconciliation_run_id=run.id, relationship_type=rel, status=MatchGroupStatus.PENDING,
        bank_aggregate_amount=agg[0], ledger_aggregate_amount=agg[1], currency_code=currency, difference=Decimal(0),
        fx_source_currency_code="USD" if fx else None, fx_target_currency_code="NGN" if fx else None,
        fx_rate=Decimal(1600) if fx else None, fx_rate_date=datetime.date(2026, 6, 1) if fx else None,
        fx_rate_type="SPOT" if fx else None, fx_rate_source="TEST" if fx else None,
        fx_tolerance_pct=Decimal(1) if fx else None, score=Decimal(100), confidence=Decimal(100),
        reason=f"{relationship} proposal", matching_rule_version="5D-1.0", group_key=f"{relationship}:{uuid.uuid4()}",
    )
    db_session.add(group)
    await db_session.flush()
    for b in bank_ids:
        db_session.add(ReconciliationMatchGroupMember(match_group_id=group.id, bank_statement_transaction_id=b))
    for t in ledger_ids:
        db_session.add(ReconciliationMatchGroupMember(match_group_id=group.id, treasury_transaction_id=t))
    await db_session.flush()
    return run.id, group.id, len(bank_ids) + len(ledger_ids)


TYPES = ["ONE_TO_MANY", "MANY_TO_ONE", "BATCH", "INTERNAL_TRANSFER", "FX_MATCH"]


@pytest.mark.parametrize("relationship", TYPES)
async def test_every_group_type_can_be_accepted_without_creating_exceptions(client, db_session, demo_group_and_entities, cash_event_types, relationship):
    from app.models.reconciliation import MatchGroupStatus, ReconciliationMatchGroup

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, f"g-acc-{relationship[:4]}", "8200000011")
    run_id, group_id, _ = await _build_group(db_session, entity, account, relationship)
    run_id, group_id = str(run_id), str(group_id)
    await db_session.commit()
    headers = await _headers(client, user)

    # Pending: listed for a decision, explicitly NOT an exception, and not accepted.
    assert [(p["label"], p["status"]) for p in await _proposals(client, headers)] == [(relationship, "PENDING")]
    assert (await client.post(f"{BASE}/runs/{run_id}/open-items/sync", headers=headers)).json()["created"] == 0
    detail = (await client.get(f"{MP}/GROUP/{group_id}", headers=headers)).json()
    assert detail["label"] == relationship and detail["status"] == "PENDING"
    assert len(detail["bank_transactions"]) + len(detail["ledger_transactions"]) >= 2  # supporting references shown
    assert (detail["fx"] is not None) == (relationship == "FX_MATCH")
    if relationship == "FX_MATCH":
        assert detail["fx"]["rate"] == "1600.0000000000" and detail["fx"]["rate_type"] == "SPOT"

    assert (await _decide(client, headers, "GROUP", group_id, "accept", "Confirmed.")).json()["status"] == "ACCEPTED"
    db_session.expire_all()
    assert (await db_session.get(ReconciliationMatchGroup, uuid.UUID(group_id))).status == MatchGroupStatus.ACCEPTED
    assert (await client.post(f"{BASE}/runs/{run_id}/open-items/sync", headers=headers)).json()["created"] == 0
    assert await _items(client, headers) == []


@pytest.mark.parametrize("relationship", TYPES)
async def test_every_group_type_rejection_surfaces_its_members_as_exceptions(client, db_session, demo_group_and_entities, cash_event_types, relationship):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, f"g-rej-{relationship[:4]}", "8200000022")
    run_id, group_id, member_count = await _build_group(db_session, entity, account, relationship)
    group_id = str(group_id)
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _decide(client, headers, "GROUP", group_id, "reject", REJECT_REASON)).json()["status"] == "REJECTED"
    items = await _items(client, headers)
    assert len(items) == member_count  # every covered transaction is an actionable exception again
    assert all(REJECT_REASON in i["description"] for i in items)
    rejected = await _proposals(client, headers, status="REJECTED", kind="GROUP")
    assert [(p["label"], p["status"]) for p in rejected] == [(relationship, "REJECTED")]


# ======================================================================================
# 9-10: idempotency and conflicts
# ======================================================================================

async def test_repeated_decisions_do_not_duplicate_events(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.audit import AuditEvent
    from app.models.reconciliation import ReconciliationMatchDecision

    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "idem", "8100000099")
    pid = (await _proposals(client, headers))[0]["id"]
    first = await _decide(client, headers, "SUGGESTION", pid, "accept")
    again = [await _decide(client, headers, "SUGGESTION", pid, "accept") for _ in range(2)]
    assert first.json()["already_decided"] is False and all(r.status_code == 200 and r.json()["already_decided"] for r in again)
    assert await _count(db_session, ReconciliationMatchDecision) == 1
    assert await _count(db_session, AuditEvent, AuditEvent.action == "MATCH_ACCEPTED") == 1


async def test_conflicting_decision_is_refused_and_changes_nothing(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import ReconciliationMatchDecision

    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "conf", "8100000100")
    pid = (await _proposals(client, headers))[0]["id"]
    await _decide(client, headers, "SUGGESTION", pid, "accept")
    conflict = await _decide(client, headers, "SUGGESTION", pid, "reject", REJECT_REASON)
    assert conflict.status_code == 409 and "final" in conflict.json()["detail"]
    assert (await _proposals(client, headers, status="ACCEPTED"))[0]["status"] == "ACCEPTED"
    assert await _items(client, headers) == [] and await _count(db_session, ReconciliationMatchDecision) == 1


async def test_concurrent_conflicting_decisions_cannot_both_succeed(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import ReconciliationMatchDecision

    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "race", "8100000111")
    pid = (await _proposals(client, headers))[0]["id"]
    results = await asyncio.gather(
        _decide(client, headers, "SUGGESTION", pid, "accept"),
        _decide(client, headers, "SUGGESTION", pid, "reject", REJECT_REASON),
        return_exceptions=True,
    )
    assert sorted(r.status_code for r in results if not isinstance(r, Exception)) == [200, 409]
    assert await _count(db_session, ReconciliationMatchDecision) == 1  # exactly one decision was ever recorded


# ======================================================================================
# 11: isolation and permissions
# ======================================================================================

async def test_cross_entity_and_cross_group_decisions_are_denied(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.entity import Group, LegalEntity
    from app.models.rbac import EntityScopeType

    entity_a, account_a, user_a, headers_a, run_a, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "isoa", "8300000011")
    entity_b, account_b, user_b = await _setup(client, db_session, demo_group_and_entities, "isob", "8300000022", entity_index=1)
    group2 = Group(name="MD Group 2", code="MDG2", reporting_currency_code="NGN")
    db_session.add(group2)
    await db_session.flush()
    db_session.add(LegalEntity(group_id=group2.id, name="MD Entity C", code="MDENTC", functional_currency_code="NGN", country="NG"))
    user_c = await _make_scoped_user(db_session, scope_type=EntityScopeType.GROUP_WIDE, group_id=group2.id, label="isoc")
    await db_session.commit()
    pid = (await _proposals(client, headers_a))[0]["id"]

    for outsider in (await _headers(client, user_b), await _headers(client, user_c)):
        assert await _proposals(client, outsider) == []
        assert (await client.get(f"{MP}/summary", headers=outsider)).json()["pending_decisions"] == 0
        for call in (client.get(f"{MP}/SUGGESTION/{pid}", headers=outsider),
                     _decide(client, outsider, "SUGGESTION", pid, "accept"),
                     _decide(client, outsider, "SUGGESTION", pid, "reject", REJECT_REASON)):
            resp = await call
            assert resp.status_code == 404 and resp.json()["detail"] == "Match proposal not found."
    assert (await _proposals(client, headers_a))[0]["status"] == "PENDING"  # untouched
    assert (await client.get(MP, params={"legal_entity_id": str(entity_a.id)}, headers=await _headers(client, user_b))).status_code == 403


async def test_decision_needs_approve_permission_but_viewing_does_not(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.rbac import TreasuryAction

    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "perm", "8100000122")
    viewer = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW, TreasuryAction.RESOLVE, TreasuryAction.CLOSE], "no-approve")
    await db_session.commit()
    vh = await _headers(client, viewer)
    pid = (await _proposals(client, vh))[0]["id"]            # can see it
    assert (await _decide(client, vh, "SUGGESTION", pid, "accept")).status_code == 403
    assert (await _decide(client, vh, "SUGGESTION", pid, "reject", REJECT_REASON)).status_code == 403
    assert (await _proposals(client, headers))[0]["status"] == "PENDING"


# ======================================================================================
# 12-13: reversed rows excluded; sync never resets worked items
# ======================================================================================

async def test_reversed_bank_transaction_stays_excluded_from_proposals_and_exceptions(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.bank_statement import BankStatementTransactionStatus

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "revd", "8400000011")
    reversed_txn = await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, bank_reference="REV")
    reversed_txn.status = BankStatementTransactionStatus.REVERSED
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="321000", bank_reference="LIVE", row_number=3)
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)
    assert [i["amount"] for i in await _items(client, headers)] == ["321000.00"]
    assert await _proposals(client, headers, status=["PENDING", "ACCEPTED", "REJECTED"]) == []


async def test_sync_after_a_decision_does_not_reset_resolved_or_closed_items(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import ReconciliationOpenItemEvent

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "noreset", "8400000022")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="900000", bank_reference="WORKED")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="2500000", bank_reference="INV-X", row_number=3)
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="2500000", direction="INFLOW", reference="INV-X")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)
    worked = (await _items(client, headers))[0]
    iid = worked["id"]
    await _post(client, headers, iid, "start-investigation")
    await _post(client, headers, iid, "resolve", {"reason": LONG})
    await _post(client, headers, iid, "close", {})
    events_before = await _count(db_session, ReconciliationOpenItemEvent, ReconciliationOpenItemEvent.open_item_id == uuid.UUID(iid))

    pid = (await _proposals(client, headers))[0]["id"]
    await _decide(client, headers, "SUGGESTION", pid, "reject", REJECT_REASON)   # triggers a re-sync
    await client.post(f"{BASE}/runs/{run_id}/open-items/sync", headers=headers)  # and an explicit one

    closed = await _items(client, headers, status=["CLOSED"])
    assert [i["id"] for i in closed] == [iid] and closed[0]["resolution_reason"] == LONG   # untouched
    assert await _count(db_session, ReconciliationOpenItemEvent, ReconciliationOpenItemEvent.open_item_id == uuid.UUID(iid)) == events_before
    assert len(await _items(client, headers)) == 2   # only the 2 exceptions created by the rejection are new


# ======================================================================================
# 14: no financial side effects
# ======================================================================================

async def test_decisions_never_change_ledger_bank_or_cash_records(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.balance import BankBalance
    from app.models.bank_statement import BankStatementTransaction
    from app.models.treasury_transaction import TreasuryTransaction

    entity, account, user, headers, run_id, _ = await _exact_match_scenario(
        client, db_session, demo_group_and_entities, "fin2", "8500000011")
    other_run, other_group, _ = await _build_group(db_session, entity, account, "ONE_TO_MANY")
    db_session.add(BankBalance(bank_account_id=account.id, balance_date=datetime.date(2026, 5, 31), currency_code="NGN",
                               closing_balance=Decimal(5000000), available_balance=Decimal(5000000), source="MANUAL"))
    other_group = str(other_group)
    await db_session.commit()

    async def snapshot():
        db_session.expire_all()
        return (
            (await db_session.execute(select(TreasuryTransaction.id, TreasuryTransaction.transaction_amount,
                                             TreasuryTransaction.status, TreasuryTransaction.direction).order_by(TreasuryTransaction.id))).all(),
            (await db_session.execute(select(BankStatementTransaction.id, BankStatementTransaction.amount,
                                             BankStatementTransaction.status).order_by(BankStatementTransaction.id))).all(),
            (await db_session.execute(select(BankBalance.id, BankBalance.closing_balance, BankBalance.available_balance))).all(),
        )

    before = await snapshot()
    pid = (await _proposals(client, headers, run_id=run_id))[0]["id"]
    await _decide(client, headers, "SUGGESTION", pid, "accept")
    await _decide(client, headers, "GROUP", other_group, "reject", REJECT_REASON)
    assert await snapshot() == before
