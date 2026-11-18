"""Stage 5G learning evidence: what counts as evidence, what is excluded and why, traceability, the cutoff, and isolation."""
import datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update

from app.models.bank_statement import BankStatementTransaction, BankStatementTransactionStatus
from app.models.reconciliation import (
    MatchDecisionType,
    MatchProposalKind,
    MatchSuggestionStatus,
    MatchType,
    ReconciliationAcceptedClaim,
    ReconciliationMatchDecision,
    ReconciliationMatchSuggestion,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.services.reconciliation_adaptive_learning import aggregate, load_evidence
from app.services.reconciliation_adaptive_settings import DEFAULT_SETTINGS
from tests.reconciliation_adaptive_world import (
    annotate,
    enabled_config,
    make_pair,
    scores,
    seed_history,
    target,
)
from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_acceptance_conflicts import _group, _suggestion
from tests.test_reconciliation_open_items import _headers, _setup

S, D, B, T, C = (ReconciliationMatchSuggestion, ReconciliationMatchDecision, BankStatementTransaction, TreasuryTransaction,
                 ReconciliationAcceptedClaim)


def _cutoff():
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=1)


async def _world(client, db_session, demo, label, number, **seed):
    entity, account, user = await _setup(client, db_session, demo, label, number)
    await db_session.commit()
    headers = await _headers(client, user)
    history = await seed_history(client, db_session, headers, entity, account, **seed)
    return entity, account, user, headers, history


async def _load(db_session, entity, account, cutoff=None, settings=DEFAULT_SETTINGS):
    return await load_evidence(db_session, entity.id, account.id, cutoff or _cutoff(), settings)


# ======================================================================================
# eligibility and traceability
# ======================================================================================
async def test_accepted_and_rejected_decisions_are_eligible_and_traceable_to_their_original_decision(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, _h, history = await _world(client, db_session, demo_group_and_entities, "le1", "9970000011", accepted=3, rejected=2)
    ev = await _load(db_session, entity, account)
    assert (len(ev.rows), sum(r.accepted for r in ev.rows), sum(not r.accepted for r in ev.rows)) == (5, 3, 2)
    assert ev.scanned == 5 and not any(ev.exclusions.values())
    decisions = {d.id: d for d in (await db_session.execute(select(D))).scalars()}
    for row in ev.rows:                                                       # every evidence row IS a real Stage 5E decision
        d = decisions[row.decision_id]
        assert (d.kind, d.reconciliation_run_id, d.legal_entity_id) == (MatchProposalKind.SUGGESTION, row.run_id, entity.id)
        assert d.decided_by_user_id is not None and d.suggestion_id in history["suggestion_ids"]
        assert (d.decision == MatchDecisionType.ACCEPTED) == row.accepted and d.created_at == row.decided_at
        assert row.signals and row.currency == "NGN" and row.direction == "CREDIT" and row.match_type == "EXACT"


async def test_pending_suggestions_are_never_evidence(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, _h, _hist = await _world(client, db_session, demo_group_and_entities, "le2", "9970000021", accepted=2)
    await target(db_session, entity, account, count=4, start=700)            # four PENDING suggestions, never decided
    ev = await _load(db_session, entity, account)
    assert len(ev.rows) == 2 and ev.scanned == 2                              # only the two explicit decisions


async def _corrupt_status_mismatch(db, ctx):
    await db.execute(update(S).where(S.id == ctx["s"].id).values(status=MatchSuggestionStatus.PENDING))


async def _corrupt_bank_reversed(db, ctx):
    await db.execute(update(B).where(B.id == ctx["s"].bank_statement_transaction_id).values(status=BankStatementTransactionStatus.REVERSED))


async def _corrupt_ledger_not_posted(db, ctx):
    other = next(s for s in TransactionStatus if s != TransactionStatus.POSTED)
    await db.execute(update(T).where(T.id == ctx["s"].treasury_transaction_id).values(status=other))


async def _corrupt_claim_missing(db, ctx):
    await db.execute(delete(C).where(C.decision_id == ctx["d"].id))


async def _corrupt_too_old(db, ctx):
    await db.execute(update(D).where(D.id == ctx["d"].id).values(created_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=700)))


async def _corrupt_other_account(db, ctx):
    other = await _make_account(db, ctx["entity"].id, (await _make_bank(db)).id, account_number="5544332211")
    await db.execute(update(T).where(T.id == ctx["s"].treasury_transaction_id).values(bank_account_id=other.id))


async def _corrupt_currency(db, ctx):
    await db.execute(update(B).where(B.id == ctx["s"].bank_statement_transaction_id).values(currency_code="USD"))


@pytest.mark.parametrize("reason, corrupt", [
    ("STATUS_MISMATCH", _corrupt_status_mismatch), ("BANK_TRANSACTION_REVERSED", _corrupt_bank_reversed),
    ("LEDGER_TRANSACTION_NOT_POSTED", _corrupt_ledger_not_posted), ("ACCEPTED_WITHOUT_CLAIM", _corrupt_claim_missing),
    ("OUTSIDE_EVIDENCE_WINDOW", _corrupt_too_old), ("SCOPE_MISMATCH", _corrupt_other_account), ("CURRENCY_MISMATCH", _corrupt_currency),
])
async def test_an_ineligible_decision_is_excluded_and_counted_by_reason(client, db_session, demo_group_and_entities, cash_event_types, reason, corrupt):
    entity, account, _user, _h, history = await _world(client, db_session, demo_group_and_entities, "le3", "9970000031", accepted=4)
    s = await db_session.get(S, history["suggestion_ids"][0])
    d = (await db_session.execute(select(D).where(D.suggestion_id == s.id))).scalar_one()
    await corrupt(db_session, {"s": s, "d": d, "entity": entity})
    await db_session.commit()
    ev = await _load(db_session, entity, account)
    assert len(ev.rows) == 3 and ev.exclusions[reason] == 1 and sum(ev.exclusions.values()) == 1
    assert d.id not in {r.decision_id for r in ev.rows}                       # the corrupted decision is never silently retained


async def test_decisions_in_a_run_that_is_not_completed_are_excluded(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, _h, history = await _world(client, db_session, demo_group_and_entities, "le4", "9970000041", accepted=4)
    await db_session.execute(update(ReconciliationRun).where(ReconciliationRun.id == history["run_ids"][0]).values(status=ReconciliationRunStatus.CANCELLED))
    await db_session.commit()
    ev = await _load(db_session, entity, account)
    assert len(ev.rows) == 2 and ev.exclusions["RUN_NOT_COMPLETED"] == 2


async def test_invalid_incomplete_and_inconsistent_decision_records_are_excluded(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, _h, history = await _world(client, db_session, demo_group_and_entities, "le5", "9970000051", accepted=2)
    run_a, run_b = history["run_ids"]
    # (1) a decision whose recorded outcome contradicts itself
    bank, ledger = await make_pair(db_session, entity, account, i=801)
    s1 = await _suggestion(db_session, await db_session.get(ReconciliationRun, run_a), bank, ledger)
    s1.status = MatchSuggestionStatus.ACCEPTED
    db_session.add(D(reconciliation_run_id=run_a, legal_entity_id=entity.id, kind=MatchProposalKind.SUGGESTION, suggestion_id=s1.id,
                     decision=MatchDecisionType.ACCEPTED, previous_status="PENDING", new_status="REJECTED", decided_by_user_id=user.test_id))
    # (2) an incomplete suggestion (no bank row) carrying an ACCEPTED decision
    _bank2, ledger2 = await make_pair(db_session, entity, account, i=802)
    s2 = S(reconciliation_run_id=run_a, bank_statement_transaction_id=None, treasury_transaction_id=ledger2.id, match_type=MatchType.EXACT,
           confidence=95, status=MatchSuggestionStatus.ACCEPTED, reason="incomplete", matching_rule_version="5C-1.0")
    db_session.add(s2)
    await db_session.flush()
    db_session.add(D(reconciliation_run_id=run_a, legal_entity_id=entity.id, kind=MatchProposalKind.SUGGESTION, suggestion_id=s2.id,
                     decision=MatchDecisionType.ACCEPTED, previous_status="PENDING", new_status="ACCEPTED", decided_by_user_id=user.test_id))
    # (3) a decision recorded against a different run than its suggestion's
    bank3, ledger3 = await make_pair(db_session, entity, account, i=803)
    s3 = await _suggestion(db_session, await db_session.get(ReconciliationRun, run_a), bank3, ledger3)
    s3.status = MatchSuggestionStatus.ACCEPTED
    db_session.add(D(reconciliation_run_id=run_b, legal_entity_id=entity.id, kind=MatchProposalKind.SUGGESTION, suggestion_id=s3.id,
                     decision=MatchDecisionType.ACCEPTED, previous_status="PENDING", new_status="ACCEPTED", decided_by_user_id=user.test_id))
    await db_session.commit()
    ev = await _load(db_session, entity, account)
    assert len(ev.rows) == 2                                                  # only the two genuine decisions
    assert (ev.exclusions["INVALID_DECISION_RECORD"], ev.exclusions["INCOMPLETE_SUGGESTION"], ev.exclusions["RUN_MISMATCH"]) == (1, 1, 1)


async def test_group_decisions_are_not_learned_from_in_v1(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, _h, history = await _world(client, db_session, demo_group_and_entities, "le6", "9970000061", accepted=2)
    run = await db_session.get(ReconciliationRun, history["run_ids"][0])
    for n in range(6):                                                         # six explicitly ACCEPTED group decisions
        b, l = await make_pair(db_session, entity, account, i=820 + n)
        g = await _group(db_session, run, "ONE_TO_MANY", [b], [l])
        db_session.add(D(reconciliation_run_id=run.id, legal_entity_id=entity.id, kind=MatchProposalKind.GROUP, group_id=g.id,
                         decision=MatchDecisionType.ACCEPTED, previous_status="PENDING", new_status="ACCEPTED", decided_by_user_id=user.test_id))
    await db_session.commit()
    ev = await _load(db_session, entity, account)
    assert len(ev.rows) == 2 and ev.exclusions["GROUP_PROPOSAL_NOT_LEARNED_IN_V1"] == 6   # counted for health, never used
    assert all(key[3] for key in aggregate(ev.rows, _cutoff(), DEFAULT_SETTINGS))


# ======================================================================================
# cutoff and immutability of the evidence
# ======================================================================================
async def test_the_evidence_cutoff_is_respected(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, _h, _history = await _world(client, db_session, demo_group_and_entities, "le7", "9970000071", accepted=4)
    decisions = sorted((await db_session.execute(select(D))).scalars(), key=lambda d: d.created_at)
    cutoff = decisions[2].created_at                                           # strictly BEFORE the third decision
    ev = await _load(db_session, entity, account, cutoff=cutoff)
    assert {r.decision_id for r in ev.rows} == {decisions[0].id, decisions[1].id} and ev.scanned == 2
    assert len((await _load(db_session, entity, account, cutoff=cutoff - datetime.timedelta(days=1))).rows) == 0
    assert len((await _load(db_session, entity, account)).rows) == 4


async def test_reading_evidence_is_repeatable_and_changes_nothing(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, _h, _hist = await _world(client, db_session, demo_group_and_entities, "le8", "9970000081", accepted=3, rejected=1)

    async def snapshot():
        return ((await db_session.execute(select(D.id, D.decision, D.created_at, D.updated_at).order_by(D.id))).all(),
                (await db_session.execute(select(S.id, S.status, S.confidence, S.updated_at).order_by(S.id))).all(),
                (await db_session.execute(select(C.id).order_by(C.id))).all())

    before = await snapshot()
    cutoff = _cutoff()
    first = await _load(db_session, entity, account, cutoff=cutoff)
    second = await _load(db_session, entity, account, cutoff=cutoff)
    assert [(r.decision_id, r.accepted, r.signals) for r in first.rows] == [(r.decision_id, r.accepted, r.signals) for r in second.rows]
    assert await snapshot() == before


# ======================================================================================
# isolation
# ======================================================================================
async def test_a_decision_recorded_against_another_entity_is_never_used(client, db_session, demo_group_and_entities, cash_event_types):
    """The entity filter is independent of the account filter: this decision sits in THIS account's run but names another entity."""
    entity, account, _user, _h, history = await _world(client, db_session, demo_group_and_entities, "le13", "9970000131", accepted=4)
    other_entity, _other_account, _ou = await _setup(client, db_session, demo_group_and_entities, "le13b", "9970000132", entity_index=1)
    await db_session.commit()
    decision = (await db_session.execute(select(D).where(D.suggestion_id == history["suggestion_ids"][0]))).scalar_one()
    await db_session.execute(update(D).where(D.id == decision.id).values(legal_entity_id=other_entity.id))
    await db_session.commit()
    ev = await _load(db_session, entity, account)
    assert len(ev.rows) == 3 and decision.id not in {r.decision_id for r in ev.rows}



async def test_one_entitys_history_never_reaches_another_entity(client, db_session, demo_group_and_entities, cash_event_types):
    ent_a, acc_a, user_a, _ha, _hist_a = await _world(client, db_session, demo_group_and_entities, "le9a", "9970000091", accepted=6)
    entity_b, account_b, user_b = await _setup(client, db_session, demo_group_and_entities, "le9b", "9970000092", entity_index=1)
    await db_session.commit()
    assert entity_b.id != ent_a.id
    ev_b = await _load(db_session, entity_b, account_b)
    assert ev_b.rows == [] and ev_b.scanned == 0                              # entity A's six decisions are invisible to entity B
    tgt = await target(db_session, entity_b, account_b)
    cfg = await enabled_config(db_session, entity_b, account=account_b)
    await annotate(db_session, tgt["run"], cfg, user_b)
    [row] = await scores(db_session, tgt["run"].id)
    assert row.fallback and row.adaptive_contribution == 0 and row.evidence == [] and row.legal_entity_id == entity_b.id
    assert row.accepted_examples == 0 and "enough evidence" in row.fallback_reason
    # and the reverse: B's decisions never reach A either
    headers_b = await _headers(client, user_b)
    await seed_history(client, db_session, headers_b, entity_b, account_b, accepted=6, start=300)
    tgt_a = await target(db_session, ent_a, acc_a, start=950)
    cfg_a = await enabled_config(db_session, ent_a, account=acc_a)
    await annotate(db_session, tgt_a["run"], cfg_a, user_a)
    [row_a] = await scores(db_session, tgt_a["run"].id)
    assert row_a.adaptive_contribution == Decimal("7.50") and row_a.accepted_examples == 6    # exactly A's own six, not twelve


async def test_one_bank_accounts_history_never_reaches_another_account_of_the_same_entity(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account1, user, _h, _hist = await _world(client, db_session, demo_group_and_entities, "le10", "9970000101", accepted=6)
    account2 = await _make_account(db_session, entity.id, (await _make_bank(db_session)).id, account_number="6655443322")
    await db_session.commit()
    ev2 = await _load(db_session, entity, account2)
    assert ev2.rows == [] and ev2.scanned == 0
    t2, t1 = await target(db_session, entity, account2), await target(db_session, entity, account1, start=960)
    cfg = await enabled_config(db_session, entity)                             # ONE entity-wide configuration covers both accounts
    await annotate(db_session, t2["run"], cfg, user)
    await annotate(db_session, t1["run"], cfg, user)
    [other], [own] = await scores(db_session, t2["run"].id), await scores(db_session, t1["run"].id)
    assert other.fallback and other.adaptive_contribution == 0 and other.bank_account_id == account2.id
    assert other.evidence == [] and other.accepted_examples == 0
    assert own.fallback is False and own.adaptive_contribution == Decimal("7.50") and own.bank_account_id == account1.id


async def test_a_different_transaction_direction_is_a_different_scope(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, _h, _hist = await _world(client, db_session, demo_group_and_entities, "le11", "9970000111", accepted=6)
    tgt = await target(db_session, entity, account, start=970, entry="DEBIT", direction="OUTFLOW")      # same pattern, opposite direction
    cfg = await enabled_config(db_session, entity, account=account)
    await annotate(db_session, tgt["run"], cfg, user)
    [row] = await scores(db_session, tgt["run"].id)
    assert row.direction == "DEBIT" and row.fallback and row.adaptive_contribution == 0 and row.evidence == []


async def test_a_different_match_type_is_a_different_scope(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, _h, _hist = await _world(client, db_session, demo_group_and_entities, "le12", "9970000121", accepted=6)
    tgt = await target(db_session, entity, account, start=980)
    tgt["suggestions"][0].match_type = MatchType.TOLERANCE                    # history is EXACT; this candidate is TOLERANCE
    await db_session.commit()
    cfg = await enabled_config(db_session, entity, account=account)
    await annotate(db_session, tgt["run"], cfg, user)
    [row] = await scores(db_session, tgt["run"].id)
    assert row.match_type == "TOLERANCE" and row.fallback and row.evidence == []
