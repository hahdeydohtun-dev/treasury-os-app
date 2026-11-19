"""
Stage 5G evidence independence: ONE underlying transaction pair is ONE learning observation, however many decisions exist about it.

Why it matters. Stage 5E lets a rejected pair return to the unmatched pool, so the same bank row and ledger row can be proposed (and
rejected) again in every later run. If each decision record counted as an independent example, five rejections of one pair would
look like five examples from five runs and could satisfy `min_evidence` and `min_distinct_runs` on their own.

Policy under test: an observation is identified by (bank transaction, ledger transaction, currency, direction, match type) within the
entity and bank account. Among that pair's ELIGIBLE decisions the LATEST is authoritative (ties broken by decision id); the earlier
ones are counted as superseded, never deleted, and their ids stay on the observation for audit. The run of the authoritative decision
is the observation's run.
"""
import datetime
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.reconciliation import MatchSuggestionStatus, MatchType
from app.models.reconciliation import ReconciliationMatchDecision as D
from app.models.reconciliation import ReconciliationMatchSuggestion as S
from app.services.reconciliation_adaptive_learning import EvidenceRow, aggregate, load_evidence
from app.services.reconciliation_adaptive_settings import DEFAULT_SETTINGS
from tests.reconciliation_adaptive_world import (
    REJECT,
    annotate,
    enabled_config,
    make_pair,
    scores,
    seed_history,
    target,
)
from tests.test_reconciliation_acceptance_conflicts import _accept, _run, _suggestion
from tests.test_reconciliation_match_decisions import _decide, _proposals
from tests.test_reconciliation_matching_engine import _make_bank_statement_txn, _make_treasury_txn
from tests.test_reconciliation_open_items import _execute, _headers, _setup

PAIR_SCOPE = ("NGN", "CREDIT", "EXACT", "date_offset", "+1d")       # any one of the four signal keys the standard pair produces
SUPERSEDED = "SUPERSEDED_BY_LATER_DECISION_ON_SAME_PAIR"


def _cutoff():
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=1)


async def _world(client, db_session, demo, label, number):
    entity, account, user = await _setup(client, db_session, demo, label, number)
    await db_session.commit()
    return entity, account, user, await _headers(client, user)


async def _decide_pair(client, db_session, headers, run, bank, ledger, outcome="reject"):
    """A suggestion for the SAME pair in `run`, decided through the real Stage 5E API. Returns (suggestion id, decision id)."""
    suggestion = await _suggestion(db_session, run, bank, ledger)
    await db_session.commit()
    resp = await (_decide(client, headers, "SUGGESTION", str(suggestion.id), "reject", REJECT) if outcome == "reject"
                  else _accept(client, headers, "SUGGESTION", str(suggestion.id)))
    assert resp.status_code == 200, resp.text
    decision = (await db_session.execute(select(D).where(D.suggestion_id == suggestion.id))).scalar_one()
    return suggestion.id, decision.id


async def _repeat(client, db_session, headers, entity, account, bank, ledger, times, outcome="reject"):
    runs = [await _run(db_session, entity, account) for _ in range(times)]
    await db_session.commit()
    return runs, [await _decide_pair(client, db_session, headers, run, bank, ledger, outcome) for run in runs]


async def _evidence(db_session, entity, account, cutoff=None):
    return await load_evidence(db_session, entity.id, account.id, cutoff or _cutoff(), DEFAULT_SETTINGS)


# ======================================================================================
# the facts: can one pair really be proposed and decided again?
# ======================================================================================
async def test_the_real_engine_proposes_a_rejected_pair_again_in_every_later_run(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, headers = await _world(client, db_session, demo_group_and_entities, "ei0", "9910000011")
    bank = await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, bank_reference="PAY-12345", narration="Supplier ABC")
    ledger = await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, direction="INFLOW", reference="PAY-12345", narration="Supplier ABC")
    pair = (bank.id, ledger.id)
    await db_session.commit()
    seen, decisions = [], []
    for _ in range(3):
        run_id = await _execute(client, headers, entity, account)
        real = [s for s in (await db_session.execute(select(S).where(S.reconciliation_run_id == uuid.UUID(run_id)))).scalars()
                if s.treasury_transaction_id is not None]
        assert len(real) == 1 and real[0].status == MatchSuggestionStatus.PENDING
        assert str(real[0].id) in {p["id"] for p in await _proposals(client, headers)}                    # decidable again, every time
        seen.append((real[0].bank_statement_transaction_id, real[0].treasury_transaction_id))
        assert (await _decide(client, headers, "SUGGESTION", str(real[0].id), "reject", REJECT)).status_code == 200
        decisions.append((await db_session.execute(select(D.id).where(D.suggestion_id == real[0].id))).scalar_one())
    assert seen == [pair, pair, pair]                       # THE SAME bank row + ledger row, in three different runs
    assert len(set(decisions)) == 3                         # three separate decision records about one underlying pair
    ev = await _evidence(db_session, entity, account)
    assert len(ev.rows) == 1 and ev.exclusions[SUPERSEDED] == 2                                           # ...which is ONE learning observation
    assert ev.rows[0].pair == pair and set(ev.rows[0].superseded_decision_ids) == set(decisions[:2]) and ev.rows[0].decision_id == decisions[2]


# ======================================================================================
# the core requirement: repeated decisions are not independent examples
# ======================================================================================
async def test_the_same_rejected_pair_in_five_runs_is_one_observation(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, headers = await _world(client, db_session, demo_group_and_entities, "ei1", "9910000021")
    bank, ledger = await make_pair(db_session, entity, account, i=1)
    await db_session.commit()
    _runs, decided = await _repeat(client, db_session, headers, entity, account, bank, ledger, 5)
    ev = await _evidence(db_session, entity, account)
    assert ev.scanned == 5                                                                  # five authoritative decision records exist...
    assert len(ev.rows) == 1 and sum(not r.accepted for r in ev.rows) == 1                  # ...but ONE rejected observation, not five
    assert ev.exclusions[SUPERSEDED] == 4
    stat = aggregate(ev.rows, _cutoff(), DEFAULT_SETTINGS)[PAIR_SCOPE]
    assert (stat.accepted, stat.rejected, len(stat.runs), len(stat.decisions)) == (0, 1, 1, 1)
    assert ev.rows[0].decision_id == decided[-1][1]                                         # the LATEST decision is authoritative


async def test_repeated_decisions_for_one_pair_cannot_satisfy_min_evidence(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers = await _world(client, db_session, demo_group_and_entities, "ei2", "9910000031")
    bank, ledger = await make_pair(db_session, entity, account, i=1)
    await db_session.commit()
    await _repeat(client, db_session, headers, entity, account, bank, ledger, 6)             # six rejections of the SAME pair, six runs
    cfg = await enabled_config(db_session, entity, account=account)
    tgt = await target(db_session, entity, account)
    await annotate(db_session, tgt["run"], cfg, user)
    [row] = await scores(db_session, tgt["run"].id)
    assert row.fallback and row.adaptive_contribution == 0 and row.recommendation_score == row.deterministic_score
    assert (row.accepted_examples, row.rejected_examples) == (0, 0)
    assert any(not p["supported"] and "only 1 earlier decision" in p["note"] for p in row.evidence)       # one observation, not six


async def test_repeated_decisions_for_one_pair_cannot_satisfy_min_distinct_runs(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers = await _world(client, db_session, demo_group_and_entities, "ei3", "9910000041")
    pairs = [await make_pair(db_session, entity, account, i=n) for n in range(5)]
    run_b = await _run(db_session, entity, account)
    run_c = await _run(db_session, entity, account)
    run_a = await _run(db_session, entity, account)
    await db_session.commit()
    p_bank, p_ledger = pairs[0]
    await _decide_pair(client, db_session, headers, run_b, p_bank, p_ledger)                  # pair 0 is rejected in run B,
    await _decide_pair(client, db_session, headers, run_c, p_bank, p_ledger)                  # again in run C,
    for bank, ledger in pairs:                                                                # then ALL FIVE pairs (pair 0 once more) in run A
        await _decide_pair(client, db_session, headers, run_a, bank, ledger)
    ev = await _evidence(db_session, entity, account)
    assert len(ev.rows) == 5 and ev.exclusions[SUPERSEDED] == 2                               # five pairs; pair 0's two older decisions superseded
    assert {r.run_id for r in ev.rows} == {run_a.id}                                         # every observation belongs to run A
    cfg = await enabled_config(db_session, entity, account=account)
    tgt = await target(db_session, entity, account)
    await annotate(db_session, tgt["run"], cfg, user)
    [row] = await scores(db_session, tgt["run"].id)
    assert row.fallback and row.adaptive_contribution == 0                                    # 5 observations satisfy min_evidence, but from ONE run
    assert any("1 run(s)" in (p["note"] or "") for p in row.evidence)                         # (B and C no longer add phantom runs)


async def test_five_genuinely_different_pairs_across_five_runs_do_satisfy_the_thresholds(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers = await _world(client, db_session, demo_group_and_entities, "ei4", "9910000051")
    await seed_history(client, db_session, headers, entity, account, accepted=5, runs=5)     # five different pairs, one per run
    ev = await _evidence(db_session, entity, account)
    assert len(ev.rows) == 5 and ev.exclusions[SUPERSEDED] == 0 and len({r.run_id for r in ev.rows}) == 5
    cfg = await enabled_config(db_session, entity, account=account)
    tgt = await target(db_session, entity, account)
    await annotate(db_session, tgt["run"], cfg, user)
    [row] = await scores(db_session, tgt["run"].id)
    assert row.fallback is False and (row.accepted_examples, row.rejected_examples) == (5, 0)
    assert row.adaptive_contribution == Decimal("7.14")                                       # 10 * 5/7


async def test_different_pairs_with_identical_signal_patterns_remain_independent_examples(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, headers = await _world(client, db_session, demo_group_and_entities, "ei5", "9910000061")
    await seed_history(client, db_session, headers, entity, account, accepted=6)
    ev = await _evidence(db_session, entity, account)
    assert len(ev.rows) == 6 and len({r.pair for r in ev.rows}) == 6 and ev.exclusions[SUPERSEDED] == 0
    assert len({tuple(sorted(r.signals.items())) for r in ev.rows}) == 1                       # identical signals, distinct underlying pairs
    stat = aggregate(ev.rows, _cutoff(), DEFAULT_SETTINGS)[PAIR_SCOPE]
    assert (stat.accepted, len(stat.decisions)) == (6, 6)


# ======================================================================================
# conflicting history, unique-pair counts and traceability
# ======================================================================================
async def test_a_pair_rejected_then_accepted_is_one_accepted_observation_and_the_conflict_is_reported(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, headers = await _world(client, db_session, demo_group_and_entities, "ei6", "9910000071")
    bank, ledger = await make_pair(db_session, entity, account, i=1)
    await db_session.commit()
    _r, decided = await _repeat(client, db_session, headers, entity, account, bank, ledger, 2, outcome="reject")
    run_c = await _run(db_session, entity, account)
    await db_session.commit()
    accepted_sid, accepted_did = await _decide_pair(client, db_session, headers, run_c, bank, ledger, outcome="accept")
    ev = await _evidence(db_session, entity, account)
    assert len(ev.rows) == 1 and ev.rows[0].accepted is True and ev.rows[0].decision_id == accepted_did   # the latest disposition decides
    assert set(ev.rows[0].superseded_decision_ids) == {decided[0][1], decided[1][1]} and ev.rows[0].conflicting is True
    assert (ev.superseded, ev.conflicting_pairs) == (2, 1)                                              # transparent: both counted and reported
    again = await _evidence(db_session, entity, account)                                               # deterministic
    assert [(r.decision_id, r.accepted, r.superseded_decision_ids) for r in again.rows] == [(r.decision_id, r.accepted, r.superseded_decision_ids) for r in ev.rows]
    assert accepted_sid is not None


def test_the_authoritative_decision_is_chosen_deterministically_even_when_timestamps_tie():
    from app.services.reconciliation_adaptive_learning import collapse_to_observations

    moment = datetime.datetime(2026, 11, 1, tzinfo=datetime.UTC)
    pair, signals = (uuid.uuid4(), uuid.uuid4()), {"date_offset": "+1d"}
    low, high = uuid.UUID(int=1), uuid.UUID(int=2)
    rows = [EvidenceRow(low, uuid.uuid4(), moment, False, "NGN", "CREDIT", "EXACT", signals, pair=pair),
            EvidenceRow(high, uuid.uuid4(), moment, True, "NGN", "CREDIT", "EXACT", signals, pair=pair)]
    for ordering in (rows, rows[::-1]):
        observations, superseded, conflicting = collapse_to_observations(ordering)
        assert len(observations) == 1 and observations[0].decision_id == high and observations[0].superseded_decision_ids == (low,)
        assert (superseded, conflicting) == (1, 1)


async def test_accepted_and_rejected_counts_reflect_unique_pairs_not_decision_records(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers = await _world(client, db_session, demo_group_and_entities, "ei7", "9910000081")
    await seed_history(client, db_session, headers, entity, account, accepted=4)              # four accepted pairs, two runs
    bank, ledger = await make_pair(db_session, entity, account, i=50)
    await db_session.commit()
    await _repeat(client, db_session, headers, entity, account, bank, ledger, 3)               # one more pair, rejected in three further runs
    ev = await _evidence(db_session, entity, account)
    assert ev.scanned == 7 and len(ev.rows) == 5 and ev.exclusions[SUPERSEDED] == 2            # 7 decision records, 5 unique pairs
    stat = aggregate(ev.rows, _cutoff(), DEFAULT_SETTINGS)[PAIR_SCOPE]
    assert (stat.accepted, stat.rejected) == (4, 1)                                           # not (4, 3)
    cfg = await enabled_config(db_session, entity, account=account)
    tgt = await target(db_session, entity, account)
    await annotate(db_session, tgt["run"], cfg, user)
    [row] = await scores(db_session, tgt["run"].id)
    assert (row.accepted_examples, row.rejected_examples) == (4, 1) and row.adaptive_contribution == Decimal("4.29")   # 10 * 3/7
    assert row.settings_snapshot["evidence_decisions_eligible"] == 5 and row.settings_snapshot["evidence_decisions_superseded"] == 2
    assert row.settings_snapshot["evidence_pairs_with_conflicting_dispositions"] == 0


async def test_evidence_references_stay_traceable_to_the_authoritative_decisions(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, headers = await _world(client, db_session, demo_group_and_entities, "ei8", "9910000091")
    await seed_history(client, db_session, headers, entity, account, accepted=4)
    bank, ledger = await make_pair(db_session, entity, account, i=50)
    await db_session.commit()
    _runs, decided = await _repeat(client, db_session, headers, entity, account, bank, ledger, 3)
    cfg = await enabled_config(db_session, entity, account=account)
    tgt = await target(db_session, entity, account)
    await annotate(db_session, tgt["run"], cfg, user)
    [row] = await scores(db_session, tgt["run"].id)
    real = {d.id: d for d in (await db_session.execute(select(D))).scalars()}
    authoritative, older = decided[-1][1], {decided[0][1], decided[1][1]}
    for pattern in row.evidence:
        assert pattern["supported"] and str(authoritative) in pattern["decision_ids"]                  # the latest decision is cited...
        assert not (older & {uuid.UUID(i) for i in pattern["decision_ids"]})                           # ...the older duplicates are not counted as evidence
        assert {uuid.UUID(i) for i in pattern["superseded_decision_ids"]} == older and pattern["repeated_decisions"] == 2
        assert all(uuid.UUID(i) in real for i in pattern["decision_ids"] + pattern["superseded_decision_ids"])   # every id is a real Stage 5E decision
        assert {real[uuid.UUID(i)].suggestion_id for i in pattern["superseded_decision_ids"]} <= {sid for sid, _d in decided}
    served = (await client.get("/api/v1/reconciliation/adaptive/explanations", headers=headers,
                               params=[("suggestion_id", str(tgt["suggestions"][0].id)), ("include_decision_ids", "true")])).json()
    cited = served["explanations"][str(tgt["suggestions"][0].id)]["patterns"][0]
    assert set(cited["superseded_decision_ids"]) == {str(i) for i in older} and cited["repeated_decisions"] == 2
    plain = (await client.get("/api/v1/reconciliation/adaptive/explanations", headers=headers, params=[("suggestion_id", str(tgt["suggestions"][0].id))])).json()
    assert "superseded_decision_ids" not in str(plain) and "decision_ids" not in str(plain)            # ids still need CONFIGURE


async def test_the_same_pair_decided_under_different_match_types_stays_in_separate_scopes(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, headers = await _world(client, db_session, demo_group_and_entities, "ei9", "9910000101")
    bank, ledger = await make_pair(db_session, entity, account, i=1)
    run_a, run_b = await _run(db_session, entity, account), await _run(db_session, entity, account)
    exact = await _suggestion(db_session, run_a, bank, ledger)
    tolerance = await _suggestion(db_session, run_b, bank, ledger)
    tolerance.match_type = MatchType.TOLERANCE
    await db_session.commit()
    for s in (exact, tolerance):
        assert (await _decide(client, headers, "SUGGESTION", str(s.id), "reject", REJECT)).status_code == 200
    ev = await _evidence(db_session, entity, account)
    assert sorted(r.match_type for r in ev.rows) == ["EXACT", "TOLERANCE"] and ev.exclusions[SUPERSEDED] == 0   # match type is part of the key
