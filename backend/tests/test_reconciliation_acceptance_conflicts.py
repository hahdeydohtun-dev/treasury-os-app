"""
Stage 5E final integrity: a bank-statement or ledger transaction must never be ACCEPTED through two
conflicting proposals (suggestions or Stage 5D groups), including across reconciliation runs.
"""
import asyncio
import datetime
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.test_reconciliation_match_decisions import MP, REJECT_REASON, _count, _decide, _proposals
from tests.test_reconciliation_matching_engine import _make_bank_statement_txn, _make_treasury_txn
from tests.test_reconciliation_open_items import BASE, _execute, _headers, _items, _setup

AUTO_REASON = "[auto-match] Exact amount; exact transaction date; exact normalized reference match."


async def _run(db_session, entity, account, start="2026-06-01", end="2026-06-30"):
    from app.models.reconciliation import ReconciliationRun, ReconciliationRunStatus

    run = ReconciliationRun(legal_entity_id=entity.id, bank_account_id=account.id,
                            period_start=datetime.date.fromisoformat(start), period_end=datetime.date.fromisoformat(end),
                            status=ReconciliationRunStatus.COMPLETED)
    db_session.add(run)
    await db_session.flush()
    return run


async def _bank(db_session, entity, account, ref, amount="1000000", row=2, **kw):
    return await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount=amount,
                                          bank_reference=ref, row_number=row, **kw)


async def _ledger(db_session, entity, account, ref, amount="1000000", direction="INFLOW", **kw):
    return await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount=amount,
                                    direction=direction, reference=ref, **kw)


async def _suggestion(db_session, run, bank, ledger, confidence="95", reason=AUTO_REASON):
    from app.models.reconciliation import (
        MatchSuggestionStatus,
        MatchType,
        ReconciliationMatchSuggestion,
    )

    s = ReconciliationMatchSuggestion(
        reconciliation_run_id=run.id, bank_statement_transaction_id=bank.id, treasury_transaction_id=ledger.id,
        match_type=MatchType.EXACT, confidence=Decimal(confidence), status=MatchSuggestionStatus.PENDING,
        reason=reason, matching_rule_version="5C-1.0",
    )
    db_session.add(s)
    await db_session.flush()
    return s


async def _group(db_session, run, relationship, banks, ledgers, status="PENDING"):
    from app.models.reconciliation import (
        MatchGroupStatus,
        MatchRelationshipType,
        ReconciliationMatchGroup,
        ReconciliationMatchGroupMember,
    )

    total = sum((b.amount for b in banks), Decimal(0)) or sum((t.transaction_amount for t in ledgers), Decimal(0))
    g = ReconciliationMatchGroup(
        reconciliation_run_id=run.id, relationship_type=MatchRelationshipType[relationship],
        status=MatchGroupStatus[status], bank_aggregate_amount=total, ledger_aggregate_amount=total,
        currency_code="NGN", difference=Decimal(0), score=Decimal(100), confidence=Decimal(100),
        fx_source_currency_code="USD" if relationship == "FX_MATCH" else None,
        fx_target_currency_code="NGN" if relationship == "FX_MATCH" else None,
        fx_rate=Decimal(1600) if relationship == "FX_MATCH" else None,
        reason=f"{relationship} proposal", matching_rule_version="5D-1.0", group_key=f"{relationship}:{uuid.uuid4()}",
    )
    db_session.add(g)
    await db_session.flush()
    for b in banks:
        db_session.add(ReconciliationMatchGroupMember(match_group_id=g.id, bank_statement_transaction_id=b.id))
    for t in ledgers:
        db_session.add(ReconciliationMatchGroupMember(match_group_id=g.id, treasury_transaction_id=t.id))
    await db_session.flush()
    return g


async def _accept(client, headers, kind, pid, reason="Verified."):
    return await _decide(client, headers, kind, pid, "accept", reason)


# --------------------------------------------------------------------------------------
# Forensic reproduction: the same bank transaction proposed in two runs
# --------------------------------------------------------------------------------------
async def test_same_bank_transaction_in_two_runs_cannot_be_accepted_twice(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-bank", "9100000011")
    bank = await _bank(db_session, entity, account, "SHARED-BANK")
    y1 = await _ledger(db_session, entity, account, "LED-1")
    y2 = await _ledger(db_session, entity, account, "LED-2")
    run1, run2 = await _run(db_session, entity, account), await _run(db_session, entity, account)
    s1 = await _suggestion(db_session, run1, bank, y1)
    s2 = await _suggestion(db_session, run2, bank, y2)
    ids = (str(s1.id), str(s2.id))
    await db_session.commit()
    headers = await _headers(client, user)
    first = await _accept(client, headers, "SUGGESTION", ids[0])
    second = await _accept(client, headers, "SUGGESTION", ids[1])
    assert first.status_code == 200
    assert second.status_code == 409, "the same bank transaction was accepted through two proposals"


async def test_real_engine_naturally_proposes_the_same_bank_transaction_in_two_runs(client, db_session, demo_group_and_entities, cash_event_types):
    """The overlap is not hypothetical: re-running reconciliation after a second matching ledger row appears
    makes Stage 5C propose the SAME bank row again, against the new ledger row (the first is already claimed)."""
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-eng", "9100000022")
    await _bank(db_session, entity, account, "INV-777", amount="2500000", txn_date="2026-06-10")
    await _ledger(db_session, entity, account, "INV-777", amount="2500000", event_date="2026-06-10")
    await db_session.commit()
    headers = await _headers(client, user)
    run1 = await _execute(client, headers, entity, account)
    await _ledger(db_session, entity, account, "INV-777", amount="2500000", event_date="2026-06-10")  # a second candidate appears
    await db_session.commit()
    run2 = await _execute(client, headers, entity, account)

    pending = [p for p in await _proposals(client, headers) if p["kind"] == "SUGGESTION"]
    assert {p["reconciliation_run_id"] for p in pending} == {run1, run2}          # one proposal in each run...
    assert len(pending) == 2 and all(p["status"] == "PENDING" for p in pending)   # ...for the same bank row, both decidable
    first = await _accept(client, headers, "SUGGESTION", pending[0]["id"])
    second = await _accept(client, headers, "SUGGESTION", pending[1]["id"])
    assert (first.status_code, second.status_code) == (200, 409)


async def _decisions(db_session, *where):
    from app.models.audit import AuditEvent
    from app.models.reconciliation import ReconciliationAcceptedClaim, ReconciliationMatchDecision

    return (await _count(db_session, ReconciliationMatchDecision), await _count(db_session, ReconciliationAcceptedClaim),
            await _count(db_session, AuditEvent, AuditEvent.action == "MATCH_ACCEPTED"))


async def _two_cross_run_suggestions(db_session, entity, account, label):
    """S1=(X,Y1) in run 1, S2=(X,Y2) in run 2: the same bank row proposed twice."""
    bank = await _bank(db_session, entity, account, f"SH-{label}")
    y1, y2 = await _ledger(db_session, entity, account, f"L1-{label}"), await _ledger(db_session, entity, account, f"L2-{label}")
    run1, run2 = await _run(db_session, entity, account), await _run(db_session, entity, account)
    return (await _suggestion(db_session, run1, bank, y1)), (await _suggestion(db_session, run2, bank, y2)), (run1, run2)


# ======================================================================================
# 1. valid: non-overlapping proposals
# ======================================================================================

async def test_non_overlapping_proposals_can_all_be_accepted(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-ok", "9200000011")
    run = await _run(db_session, entity, account)
    s1 = await _suggestion(db_session, run, await _bank(db_session, entity, account, "OK-1"), await _ledger(db_session, entity, account, "OK-1"))
    s2 = await _suggestion(db_session, run, await _bank(db_session, entity, account, "OK-2", row=3), await _ledger(db_session, entity, account, "OK-2"))
    banks = [await _bank(db_session, entity, account, "OK-G", amount="10000000", row=4)]
    ledgers = [await _ledger(db_session, entity, account, "OK-G", amount=a) for a in ("4000000", "6000000")]
    g = await _group(db_session, run, "ONE_TO_MANY", banks, ledgers)
    ids = [("SUGGESTION", str(s1.id)), ("SUGGESTION", str(s2.id)), ("GROUP", str(g.id))]
    await db_session.commit()
    headers = await _headers(client, user)
    assert [(await _accept(client, headers, k, i)).status_code for k, i in ids] == [200, 200, 200]
    assert (await _decisions(db_session))[:2] == (3, 2 + 2 + 3)   # 3 decisions; 2+2 suggestion claims + 3 group members


# ======================================================================================
# 2-5. conflicts
# ======================================================================================

async def test_two_suggestions_sharing_a_ledger_transaction_cannot_both_be_accepted(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-led", "9200000022")
    ledger = await _ledger(db_session, entity, account, "SHARED-LEDGER")
    run1, run2 = await _run(db_session, entity, account), await _run(db_session, entity, account)
    s1 = await _suggestion(db_session, run1, await _bank(db_session, entity, account, "B1"), ledger)
    s2 = await _suggestion(db_session, run2, await _bank(db_session, entity, account, "B2", row=3), ledger)
    ids = (str(s1.id), str(s2.id))
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "SUGGESTION", ids[0])).status_code == 200
    resp = await _accept(client, headers, "SUGGESTION", ids[1])
    assert resp.status_code == 409 and "ledger transaction" in resp.json()["detail"] and "accepted" in resp.json()["detail"]


@pytest.mark.parametrize("accept_group_first", [False, True])
async def test_suggestion_and_group_cannot_both_be_accepted_when_they_claim_the_same_transaction(client, db_session, demo_group_and_entities, cash_event_types, accept_group_first):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, f"cx-sg{int(accept_group_first)}", "9200000033")
    bank = await _bank(db_session, entity, account, "SG", amount="10000000")
    other_ledgers = [await _ledger(db_session, entity, account, "SGL", amount=a) for a in ("4000000", "6000000")]
    run1, run2 = await _run(db_session, entity, account), await _run(db_session, entity, account)
    suggestion = await _suggestion(db_session, run1, bank, await _ledger(db_session, entity, account, "SGS", amount="10000000"))
    group = await _group(db_session, run2, "ONE_TO_MANY", [bank], other_ledgers)   # shares the BANK row with the suggestion
    order = [("GROUP", str(group.id)), ("SUGGESTION", str(suggestion.id))]
    if not accept_group_first:
        order.reverse()
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, *order[0])).status_code == 200
    assert (await _accept(client, headers, *order[1])).status_code == 409
    assert (await _decisions(db_session))[0] == 1


@pytest.mark.parametrize("shared", ["ledger", "bank"])
async def test_overlapping_groups_cannot_both_be_accepted(client, db_session, demo_group_and_entities, cash_event_types, shared):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, f"cx-gg-{shared}", "9200000044")
    run1, run2 = await _run(db_session, entity, account), await _run(db_session, entity, account)
    if shared == "ledger":      # two ONE_TO_MANY groups sharing one ledger row
        l1, l2, l3 = [await _ledger(db_session, entity, account, f"GG{i}", amount="5000000") for i in range(3)]
        b1, b2 = await _bank(db_session, entity, account, "GGB1", amount="10000000"), await _bank(db_session, entity, account, "GGB2", amount="10000000", row=3)
        g1, g2 = await _group(db_session, run1, "ONE_TO_MANY", [b1], [l1, l2]), await _group(db_session, run2, "ONE_TO_MANY", [b2], [l2, l3])
    else:                       # two MANY_TO_ONE groups sharing one bank row
        b1, b2, b3 = [await _bank(db_session, entity, account, f"GM{i}", amount="5000000", row=2 + i) for i in range(3)]
        t1, t2 = await _ledger(db_session, entity, account, "GMT1", amount="10000000"), await _ledger(db_session, entity, account, "GMT2", amount="10000000")
        g1, g2 = await _group(db_session, run1, "MANY_TO_ONE", [b1, b2], [t1]), await _group(db_session, run2, "MANY_TO_ONE", [b2, b3], [t2])
    ids = (str(g1.id), str(g2.id))
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "GROUP", ids[0])).status_code == 200
    assert (await _accept(client, headers, "GROUP", ids[1])).status_code == 409


async def test_acceptance_in_one_run_is_detected_in_a_later_run_and_creates_no_false_exceptions(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-later", "9200000055")
    await _bank(db_session, entity, account, "INV-LATER", amount="2500000", txn_date="2026-06-10")
    await _ledger(db_session, entity, account, "INV-LATER", amount="2500000", event_date="2026-06-10")
    await db_session.commit()
    headers = await _headers(client, user)
    run1 = await _execute(client, headers, entity, account)
    pid = (await _proposals(client, headers, run_id=run1))[0]["id"]
    assert (await _accept(client, headers, "SUGGESTION", pid)).status_code == 200
    run2 = await _execute(client, headers, entity, account)       # a newly executed run over the same period
    # the accepted pair is reconciled: the new run raises no false exception and no decidable proposal for it
    assert await _items(client, headers, run_id=run2) == []
    assert [p for p in await _proposals(client, headers, run_id=run2)] == []


async def test_a_pending_proposal_in_another_run_is_shown_as_covering_not_as_unmatched(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-cover", "9200000066")
    await _bank(db_session, entity, account, "INV-COVER", amount="2500000", txn_date="2026-06-10")
    await _ledger(db_session, entity, account, "INV-COVER", amount="2500000", event_date="2026-06-10")
    await db_session.commit()
    headers = await _headers(client, user)
    await _execute(client, headers, entity, account)
    run2 = await _execute(client, headers, entity, account)
    assert await _items(client, headers, run_id=run2) == []   # represented by run 1's proposal in the review queue
    assert (await client.get(f"{MP}/summary", headers=headers)).json()["pending_decisions"] == 1


# ======================================================================================
# 6. concurrency (PostgreSQL-backed)
# ======================================================================================

async def test_concurrent_conflicting_acceptances_produce_exactly_one_success(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import ReconciliationMatchSuggestion

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-race", "9300000011")
    s1, s2, _ = await _two_cross_run_suggestions(db_session, entity, account, "race")
    ids = (str(s1.id), str(s2.id))
    await db_session.commit()
    headers = await _headers(client, user)
    results = await asyncio.gather(*[_accept(client, headers, "SUGGESTION", i) for i in ids], return_exceptions=True)
    assert sorted(r.status_code for r in results if not isinstance(r, Exception)) == [200, 409]
    assert await _decisions(db_session) == (1, 2, 1)
    statuses = sorted((await db_session.execute(select(ReconciliationMatchSuggestion.status).where(
        ReconciliationMatchSuggestion.id.in_([uuid.UUID(i) for i in ids])))).scalars().all(), key=lambda x: x.value)
    assert [x.value for x in statuses] == ["ACCEPTED", "PENDING"]   # the loser is untouched, not auto-rejected


async def test_concurrent_conflicting_suggestion_and_group_produce_exactly_one_success(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-race2", "9300000022")
    bank = await _bank(db_session, entity, account, "R2", amount="10000000")
    run1, run2 = await _run(db_session, entity, account), await _run(db_session, entity, account)
    s = await _suggestion(db_session, run1, bank, await _ledger(db_session, entity, account, "R2S", amount="10000000"))
    g = await _group(db_session, run2, "ONE_TO_MANY", [bank], [await _ledger(db_session, entity, account, "R2G", amount=a) for a in ("4000000", "6000000")])
    pair = [("SUGGESTION", str(s.id)), ("GROUP", str(g.id))]
    await db_session.commit()
    headers = await _headers(client, user)
    results = await asyncio.gather(*[_accept(client, headers, k, i) for k, i in pair], return_exceptions=True)
    assert sorted(r.status_code for r in results if not isinstance(r, Exception)) == [200, 409]
    assert (await _decisions(db_session))[0] == 1


async def test_database_blocks_a_racing_acceptance_even_when_the_precheck_is_stale(client, db_session, demo_group_and_entities, cash_event_types):
    """
    Deterministic proof of the DB-level guarantee, using two REAL PostgreSQL connections. The application
    pre-check is disabled for both requests (simulating both having read stale state). Session A accepts but does not
    commit yet; session B then tries to accept an overlapping proposal and must BLOCK on A's uncommitted claim; once A
    commits, B fails with 409 and writes nothing.
    """
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from app.db.session import AsyncSessionLocal
    from app.models.rbac import User
    from app.models.reconciliation import (
        MatchDecisionType,
        MatchProposalKind,
        MatchSuggestionStatus,
        ReconciliationMatchSuggestion,
    )
    from app.services.reconciliation_match_decision_service import decide_proposal

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-db", "9300000033")
    s1, s2, _ = await _two_cross_run_suggestions(db_session, entity, account, "db")
    id1, id2, uid = s1.id, s2.id, user.test_id
    await db_session.commit()

    stale = patch("app.services.reconciliation_match_decision_service.find_acceptance_conflict", new=AsyncMock(return_value=None))
    with stale:
        async with AsyncSessionLocal() as a, AsyncSessionLocal() as b:
            actor_a, actor_b = await a.get(User, uid), await b.get(User, uid)
            await decide_proposal(a, kind=MatchProposalKind.SUGGESTION, proposal_id=id1, actor=actor_a,
                                  decision=MatchDecisionType.ACCEPTED, reason="A")      # A holds its claims, uncommitted
            task = asyncio.create_task(decide_proposal(b, kind=MatchProposalKind.SUGGESTION, proposal_id=id2, actor=actor_b,
                                                       decision=MatchDecisionType.ACCEPTED, reason="B"))
            await asyncio.sleep(1.0)
            assert not task.done(), "B should be blocked on A's uncommitted claim row, not racing past it"
            await a.commit()                                                           # release A
            with pytest.raises(HTTPException) as caught:
                await asyncio.wait_for(task, timeout=10)
            assert caught.value.status_code == 409
            await b.rollback()

    db_session.expire_all()
    assert await _decisions(db_session) == (1, 2, 1)
    loser = await db_session.get(ReconciliationMatchSuggestion, id2)
    assert loser.status == MatchSuggestionStatus.PENDING


# ======================================================================================
# 7-9. a conflict changes nothing; idempotency/409 preserved
# ======================================================================================

async def test_conflict_writes_no_decision_claim_or_audit_event_and_changes_no_source_data(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.balance import BankBalance
    from app.models.bank_statement import BankStatementTransaction
    from app.models.reconciliation import MatchSuggestionStatus, ReconciliationMatchSuggestion
    from app.models.treasury_transaction import TreasuryTransaction

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-nochange", "9300000044")
    s1, s2, _ = await _two_cross_run_suggestions(db_session, entity, account, "nc")
    ids = (str(s1.id), str(s2.id))
    db_session.add(BankBalance(bank_account_id=account.id, balance_date=datetime.date(2026, 5, 31), currency_code="NGN",
                               closing_balance=Decimal(5000000), available_balance=Decimal(5000000), source="MANUAL"))
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "SUGGESTION", ids[0])).status_code == 200

    async def snapshot():
        db_session.expire_all()
        return (
            (await db_session.execute(select(TreasuryTransaction.id, TreasuryTransaction.transaction_amount, TreasuryTransaction.status).order_by(TreasuryTransaction.id))).all(),
            (await db_session.execute(select(BankStatementTransaction.id, BankStatementTransaction.amount, BankStatementTransaction.status).order_by(BankStatementTransaction.id))).all(),
            (await db_session.execute(select(BankBalance.id, BankBalance.closing_balance, BankBalance.available_balance))).all(),
            await _decisions(db_session),
        )

    before = await snapshot()
    assert (await _accept(client, headers, "SUGGESTION", ids[1])).status_code == 409
    assert await snapshot() == before                                        # nothing at all changed
    loser = await db_session.get(ReconciliationMatchSuggestion, uuid.UUID(ids[1]))
    assert loser.status == MatchSuggestionStatus.PENDING


async def test_idempotency_and_409_semantics_are_preserved(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-idem", "9300000055")
    s1, s2, _ = await _two_cross_run_suggestions(db_session, entity, account, "idem")
    ids = (str(s1.id), str(s2.id))
    await db_session.commit()
    headers = await _headers(client, user)
    first = await _accept(client, headers, "SUGGESTION", ids[0])
    again = await _accept(client, headers, "SUGGESTION", ids[0])
    assert first.json()["already_decided"] is False and again.status_code == 200 and again.json()["already_decided"] is True
    assert (await _decide(client, headers, "SUGGESTION", ids[0], "reject", REJECT_REASON)).status_code == 409   # opposite decision
    assert (await _accept(client, headers, "SUGGESTION", ids[1])).status_code == 409                            # overlapping proposal
    assert (await _accept(client, headers, "SUGGESTION", ids[1])).status_code == 409                            # still, consistently
    assert await _decisions(db_session) == (1, 2, 1)


async def test_a_conflicted_proposal_can_still_be_rejected_and_the_detail_says_why(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-detail", "9300000066")
    s1, s2, _ = await _two_cross_run_suggestions(db_session, entity, account, "detail")
    ids = (str(s1.id), str(s2.id))
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await client.get(f"{MP}/SUGGESTION/{ids[1]}", headers=headers)).json()["acceptance_conflict"] is None
    await _accept(client, headers, "SUGGESTION", ids[0])
    detail = (await client.get(f"{MP}/SUGGESTION/{ids[1]}", headers=headers)).json()
    assert detail["status"] == "PENDING" and "already part of an accepted" in detail["acceptance_conflict"]
    rejected = await _decide(client, headers, "SUGGESTION", ids[1], "reject", REJECT_REASON)
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
    assert (await _decisions(db_session))[1] == 2          # rejection added no claims


# ======================================================================================
# 10. rejection -> exceptions only where genuinely unmatched
# ======================================================================================

async def test_rejection_raises_exceptions_only_for_transactions_no_other_proposal_covers(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-rej", "9400000011")
    s1, s2, (run1, run2) = await _two_cross_run_suggestions(db_session, entity, account, "rej")
    s1_id, run1_id = str(s1.id), str(run1.id)
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _decide(client, headers, "SUGGESTION", s1_id, "reject", REJECT_REASON)).status_code == 200
    items = await _items(client, headers, run_id=run1_id)
    # Y1 is now genuinely unmatched; X is still covered by run 2's pending proposal, so it is NOT a false exception.
    assert [i["category"] for i in items] == ["LEDGER_ONLY"] and REJECT_REASON in items[0]["description"]
    assert [p["id"] for p in await _proposals(client, headers)] == [str(s2.id)]


# ======================================================================================
# 11. all five relationship types
# ======================================================================================

async def _overlapping_pair(db_session, entity, account, relationship):
    run1, run2 = await _run(db_session, entity, account), await _run(db_session, entity, account)
    fx = relationship == "FX_MATCH"
    cur = {"currency": "USD"} if fx else {}
    n = lambda tag: f"{relationship[:4]}-{tag}"
    if relationship == "MANY_TO_ONE":        # shared: the bank row b2
        b1, b2, b3 = [await _bank(db_session, entity, account, n(f"B{i}"), amount="5000000", row=2 + i) for i in range(3)]
        t1, t2 = await _ledger(db_session, entity, account, n("T1"), amount="10000000"), await _ledger(db_session, entity, account, n("T2"), amount="10000000")
        return await _group(db_session, run1, relationship, [b1, b2], [t1]), await _group(db_session, run2, relationship, [b2, b3], [t2])
    if relationship == "INTERNAL_TRANSFER":  # shared: the middle leg
        l1, l2, l3 = [await _ledger(db_session, entity, account, n(f"L{i}"), amount="500000", direction=d) for i, d in enumerate(("OUTFLOW", "INFLOW", "OUTFLOW"))]
        return await _group(db_session, run1, relationship, [], [l1, l2]), await _group(db_session, run2, relationship, [], [l2, l3])
    # ONE_TO_MANY / BATCH / FX_MATCH: shared: one ledger row
    amount = "1600000" if fx else "10000000"
    b1 = await _bank(db_session, entity, account, n("B1"), amount="1000" if fx else "10000000", **cur)
    b2 = await _bank(db_session, entity, account, n("B2"), amount="1000" if fx else "10000000", row=3, **cur)
    shared = await _ledger(db_session, entity, account, n("S"), amount=amount)
    l_a, l_b = await _ledger(db_session, entity, account, n("A"), amount=amount), await _ledger(db_session, entity, account, n("B"), amount=amount)
    return await _group(db_session, run1, relationship, [b1], [shared, l_a]), await _group(db_session, run2, relationship, [b2], [shared, l_b])


@pytest.mark.parametrize("relationship", ["ONE_TO_MANY", "MANY_TO_ONE", "BATCH", "INTERNAL_TRANSFER", "FX_MATCH"])
async def test_every_group_type_rejects_an_overlapping_second_acceptance(client, db_session, demo_group_and_entities, cash_event_types, relationship):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, f"cx-t-{relationship[:4]}", "9500000011")
    g1, g2 = await _overlapping_pair(db_session, entity, account, relationship)
    ids = (str(g1.id), str(g2.id))
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "GROUP", ids[0])).status_code == 200
    resp = await _accept(client, headers, "GROUP", ids[1])
    assert resp.status_code == 409, (relationship, resp.text)
    assert (await _decisions(db_session))[0] == 1


async def test_a_one_to_one_match_and_the_transfer_pairing_that_includes_its_leg_are_compatible(client, db_session, demo_group_and_entities, cash_event_types):
    """Stage 5D defines the transfer group as context about an already-matched ledger leg, so they may both be accepted."""
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-xfer", "9500000022")
    run = await _run(db_session, entity, account)
    bank = await _bank(db_session, entity, account, "XFER-B", amount="500000")
    primary = await _ledger(db_session, entity, account, "XFER-P", amount="500000", direction="OUTFLOW")
    other = await _ledger(db_session, entity, account, "XFER-O", amount="500000", direction="INFLOW")
    suggestion = await _suggestion(db_session, run, bank, primary)
    transfer = await _group(db_session, run, "INTERNAL_TRANSFER", [], [primary, other])
    ids = (str(suggestion.id), str(transfer.id))
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "SUGGESTION", ids[0])).status_code == 200
    assert (await _accept(client, headers, "GROUP", ids[1])).status_code == 200      # different namespace: compatible


# ======================================================================================
# eligibility
# ======================================================================================

async def test_acceptance_is_refused_when_a_member_transaction_is_no_longer_eligible(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.bank_statement import BankStatementTransactionStatus
    from app.models.reconciliation import MatchSuggestionStatus, ReconciliationMatchSuggestion
    from app.models.treasury_transaction import TransactionStatus

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "cx-elig", "9600000011")
    run = await _run(db_session, entity, account)
    bank_a, led_a = await _bank(db_session, entity, account, "E-A"), await _ledger(db_session, entity, account, "E-A")
    bank_b, led_b = await _bank(db_session, entity, account, "E-B", row=3), await _ledger(db_session, entity, account, "E-B")
    s_rev, s_can = await _suggestion(db_session, run, bank_a, led_a), await _suggestion(db_session, run, bank_b, led_b)
    bank_a.status = BankStatementTransactionStatus.REVERSED    # reversed after the engine proposed the match
    led_b.status = TransactionStatus.CANCELLED
    ids = (s_rev.id, s_can.id)
    await db_session.commit()
    headers = await _headers(client, user)
    for pid in ids:
        resp = await _accept(client, headers, "SUGGESTION", pid)
        assert resp.status_code == 409 and "no longer eligible" in resp.json()["detail"]
    assert await _decisions(db_session) == (0, 0, 0)
    for pid in ids:
        assert (await db_session.get(ReconciliationMatchSuggestion, pid)).status == MatchSuggestionStatus.PENDING


# ======================================================================================
# internal-transfer rejection semantics (section 5): traced, not assumed
# ======================================================================================

async def _transfer_scenario(client, db_session, demo_group_and_entities, label, number, pair_in_other_account):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, label, number)
    bank = await _make_bank(db_session)
    other_account = await _make_account(db_session, entity.id, bank.id, account_number=number[:-1] + "9")
    run = await _run(db_session, entity, account)
    stmt = await _bank(db_session, entity, account, f"{label}-B", amount="500000")
    primary = await _ledger(db_session, entity, account, f"{label}-P", amount="500000", direction="OUTFLOW")
    pair_leg = await _ledger(db_session, entity, other_account if pair_in_other_account else account, f"{label}-Q",
                             amount="500000", direction="INFLOW")
    suggestion = await _suggestion(db_session, run, stmt, primary)
    transfer = await _group(db_session, run, "INTERNAL_TRANSFER", [], [primary, pair_leg])
    run_other = await _run(db_session, entity, other_account)
    return entity, user, run, run_other, suggestion, transfer, primary, pair_leg


from tests.test_bank_statement_ingestion import _make_account, _make_bank


async def test_rejecting_a_transfer_keeps_a_covered_leg_represented_and_raises_an_exception_for_an_uncovered_one(client, db_session, demo_group_and_entities, cash_event_types):
    entity, user, run, run_other, suggestion, transfer, primary, pair_leg = await _transfer_scenario(
        client, db_session, demo_group_and_entities, "xr-same", "9700000011", pair_in_other_account=False)
    ids = {"run": str(run.id), "transfer": str(transfer.id), "suggestion": str(suggestion.id), "pair": str(pair_leg.id)}
    await db_session.commit()
    headers = await _headers(client, user)
    resp = await _decide(client, headers, "GROUP", ids["transfer"], "reject", REJECT_REASON)
    assert resp.status_code == 200
    items = await _items(client, headers, run_id=ids["run"])
    # primary leg: still covered by its PENDING one-to-one proposal -> represented in the review queue, no exception.
    # pair leg: covered by nothing now -> an actionable LEDGER_ONLY exception, carrying the rejection reason.
    assert [(i["category"], i["treasury_transaction_id"]) for i in items] == [("LEDGER_ONLY", ids["pair"])]
    assert REJECT_REASON in items[0]["description"]
    assert [p["id"] for p in await _proposals(client, headers)] == [ids["suggestion"]]
    rejected = await _proposals(client, headers, status="REJECTED", kind="GROUP")
    assert rejected[0]["decision"]["reason"] == REJECT_REASON                       # the decision stays in the record


async def test_a_transfer_leg_in_another_account_is_represented_by_the_proposal_and_surfaces_in_its_own_runs_exceptions_once_rejected(client, db_session, demo_group_and_entities, cash_event_types):
    entity, user, run, run_other, suggestion, transfer, primary, pair_leg = await _transfer_scenario(
        client, db_session, demo_group_and_entities, "xr-other", "9700000022", pair_in_other_account=True)
    ids = {"run": str(run.id), "other": str(run_other.id), "transfer": str(transfer.id), "pair": str(pair_leg.id)}
    await db_session.commit()
    headers = await _headers(client, user)
    # While the transfer proposal is PENDING, the other account's run does not call the leg unmatched: it is covered by the proposal.
    assert (await client.post(f"{BASE}/runs/{ids['other']}/open-items/sync", headers=headers)).json()["created"] == 0
    assert (await _decide(client, headers, "GROUP", ids["transfer"], "reject", REJECT_REASON)).status_code == 200
    assert await _items(client, headers, run_id=ids["run"]) == []                    # out of THIS run's account scope
    # ...and once rejected, the leg is genuinely uncovered, so its own account's run surfaces it - not silently lost.
    assert (await client.post(f"{BASE}/runs/{ids['other']}/open-items/sync", headers=headers)).json()["created"] == 1
    items = await _items(client, headers, run_id=ids["other"])
    assert [(i["category"], i["treasury_transaction_id"]) for i in items] == [("LEDGER_ONLY", ids["pair"])]
