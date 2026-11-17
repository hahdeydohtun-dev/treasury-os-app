"""
Stage 5F reporting-integrity corrections, built from real Stage 5B-5E records and helpers (no reporting-only data model).

1. Run-specific attribution: in the run summary a run's accepted / proposed-only / unmatched buckets come from THAT run's
   decisions, suggestions and groups only - never from another run over the same account and an overlapping period.
2. Consistent pending semantics: "proposed only" and "pending proposals" both mean an ACTIONABLE pending proposal (one the
   Stage 5E match-review queue can actually accept or reject). Pending suggestions that remain investigation exceptions
   (review tier, competing-candidate ties, incomplete candidates) are reported separately, never as awaiting a decision.
"""
from sqlalchemy import select

from tests.reconciliation_reports_world import JUNE, kpis, report, table_rows
from tests.test_reconciliation_acceptance_conflicts import (
    _accept,
    _bank,
    _group,
    _ledger,
    _run,
    _suggestion,
)
from tests.test_reconciliation_match_decisions import MP, _decide, _proposals
from tests.test_reconciliation_open_items import _headers, _setup

DAY = "2026-06-12"
REJECT = "Customer confirmed this receipt belongs to a different invoice."


def _buckets(row):
    return {"bank": (row["accepted_bank"], row["proposed_bank"], row["unmatched_bank"], row["bank_in_scope"]),
            "ledger": (row["accepted_ledger"], row["proposed_ledger"], row["unmatched_ledger"], row["ledger_in_scope"])}


async def _two_overlapping_runs(client, db_session, demo, label, number):
    """
    Two COMPLETED runs, same entity and bank account, overlapping periods, different proposal/decision histories.
      Run A: pending  SA1 (BX,LX)  SA2 (BY,LYa)  SA3 (BZ,LZ)
      Run B: ACCEPTED SB1 (BY,LYb)   pending SB2 (BW,LW)   REJECTED SB3 (BR,LR)
      BU / LU appear in no proposal at all.   BY is accepted in B but only proposed in A.   BX,BZ are proposed in A only.
    """
    entity, account, user = await _setup(client, db_session, demo, label, number)
    run_a = await _run(db_session, entity, account, "2026-06-01", "2026-06-30")
    run_b = await _run(db_session, entity, account, "2026-06-10", "2026-07-15")        # overlaps A; both cover 2026-06-12
    b = {k: await _bank(db_session, entity, account, f"{label}-B{k}", amount="100", row=2 + i, txn_date=DAY)
         for i, k in enumerate(("X", "Y", "Z", "W", "U", "R"))}
    ledger = {k: await _ledger(db_session, entity, account, f"{label}-L{k}", amount="100", event_date=DAY) for k in ("X", "Ya", "Yb", "Z", "W", "U", "R")}
    sa = [await _suggestion(db_session, run_a, b["X"], ledger["X"]), await _suggestion(db_session, run_a, b["Y"], ledger["Ya"]),
          await _suggestion(db_session, run_a, b["Z"], ledger["Z"])]
    sb1 = await _suggestion(db_session, run_b, b["Y"], ledger["Yb"])
    sb2 = await _suggestion(db_session, run_b, b["W"], ledger["W"])
    sb3 = await _suggestion(db_session, run_b, b["R"], ledger["R"])
    ids = {"run_a": str(run_a.id), "run_b": str(run_b.id), "sb1": str(sb1.id), "sb2": str(sb2.id), "sb3": str(sb3.id), "sa": [str(s.id) for s in sa]}
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "SUGGESTION", ids["sb1"])).status_code == 200
    assert (await _decide(client, headers, "SUGGESTION", ids["sb3"], "reject", REJECT)).status_code == 200
    return {**ids, "headers": headers}


async def _runs(client, w):
    rows = {r["id"]: r for r in table_rows((await report(client, w["headers"], "run-summary")).json(), "runs")}
    return rows[w["run_a"]], rows[w["run_b"]]


# ======================================================================================
# Issue 1: run-specific attribution
# ======================================================================================
async def test_a_run_does_not_inherit_another_runs_accepted_or_pending_proposals(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _two_overlapping_runs(client, db_session, demo_group_and_entities, "ra", "9940000011")
    a, b = await _runs(client, w)
    # Run A has NO decisions: BY / LYb were accepted in run B and must not appear accepted here.
    assert _buckets(a) == {"bank": (0, 3, 3, 6), "ledger": (0, 3, 4, 7)}      # proposed: BX,BY,BZ / LX,LYa,LZ
    # Run B accepted exactly SB1 (BY, LYb); BX,BZ,LX,LYa,LZ were pending in run A only and must not appear proposed here.
    assert _buckets(b) == {"bank": (1, 1, 4, 6), "ledger": (1, 1, 5, 7)}      # proposed: BW / LW only
    assert (a["pending_proposals"], a["rejected_proposals"]) == (3, 0)
    assert (b["pending_proposals"], b["rejected_proposals"]) == (1, 1)         # SB2 pending; SB3 rejected (and NOT counted as accepted)


async def test_each_runs_buckets_add_up_and_pending_counts_agree_with_proposed_only(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _two_overlapping_runs(client, db_session, demo_group_and_entities, "rb", "9940000021")
    for row in await _runs(client, w):
        for side, (accepted, proposed, unmatched, eligible) in _buckets(row).items():
            assert accepted + proposed + unmatched == eligible, (row["reference"], side)        # exactly one bucket each
        # every pending proposal here is a one-to-one suggestion over one bank and one ledger row, so the
        # proposed-only buckets must equal the number of pending proposals on each side
        assert row["proposed_bank"] == row["proposed_ledger"] == row["pending_proposals"], row["reference"]


async def test_rejected_proposals_stay_rejected_and_are_not_counted_as_accepted(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _two_overlapping_runs(client, db_session, demo_group_and_entities, "rc", "9940000031")
    a, b = await _runs(client, w)
    assert b["rejected_proposals"] == 1 and a["rejected_proposals"] == 0
    assert b["accepted_bank"] == 1 and b["accepted_ledger"] == 1        # only SB1; the rejected pair (BR, LR) is unmatched, not accepted
    pending_ids = {p["id"] for p in await _proposals(client, w["headers"])}
    assert w["sb3"] not in pending_ids and w["sb1"] not in pending_ids   # neither the rejected nor the accepted one is awaiting a decision
    assert pending_ids == set(w["sa"]) | {w["sb2"]}


async def test_global_matching_figures_keep_their_transaction_level_meaning(client, db_session, demo_group_and_entities, cash_event_types):
    """The cross-run (global) reports count a transaction by whether it has an accepted claim ANYWHERE; unchanged."""
    w = await _two_overlapping_runs(client, db_session, demo_group_and_entities, "rd", "9940000041")
    perf = (await report(client, w["headers"], "matching-performance")).json()
    buckets = {r["side"]: r for r in table_rows(perf, "buckets")}
    assert (buckets["Bank statement rows"]["accepted"], buckets["Bank statement rows"]["proposed"], buckets["Bank statement rows"]["unmatched"]) == (1, 3, 2)
    assert (buckets["Ledger rows"]["accepted"], buckets["Ledger rows"]["proposed"], buckets["Ledger rows"]["unmatched"]) == (1, 4, 2)
    assert kpis(perf)["completion"] == "15.38"                           # 2 accepted / 13 eligible, as before


# ======================================================================================
# Issue 2: consistent pending-proposal semantics
# ======================================================================================
async def _classification_world(client, db_session, demo, label, number):
    from app.models.reconciliation import (
        MatchSuggestionStatus,
        MatchType,
        ReconciliationMatchSuggestion,
    )

    entity, account, user = await _setup(client, db_session, demo, label, number)
    run = await _run(db_session, entity, account, *JUNE)
    bank = {k: await _bank(db_session, entity, account, f"{label}-B{k}", amount="100", row=2 + i, txn_date=DAY)
            for i, k in enumerate(("auto", "review", "tie", "acc", "rej", "grp", "none"))}
    led = {k: await _ledger(db_session, entity, account, f"{label}-L{k}", amount="100", event_date=DAY)
           for k in ("auto", "review", "tieA", "tieB", "acc", "rej", "grpA", "grpB", "inc", "none")}
    s_auto = await _suggestion(db_session, run, bank["auto"], led["auto"])                                           # actionable, pending
    s_review = await _suggestion(db_session, run, bank["review"], led["review"], reason="[review] weak evidence; amount and date only")
    s_tie_a = await _suggestion(db_session, run, bank["tie"], led["tieA"])                                           # competing candidates
    s_tie_b = await _suggestion(db_session, run, bank["tie"], led["tieB"])
    s_acc = await _suggestion(db_session, run, bank["acc"], led["acc"])                                              # will be accepted
    s_rej = await _suggestion(db_session, run, bank["rej"], led["rej"])                                              # will be rejected
    group = await _group(db_session, run, "ONE_TO_MANY", [bank["grp"]], [led["grpA"], led["grpB"]])                  # pending grouped proposal
    incomplete = ReconciliationMatchSuggestion(
        reconciliation_run_id=run.id, bank_statement_transaction_id=None, treasury_transaction_id=led["inc"].id,
        match_type=MatchType.EXACT, confidence=95, status=MatchSuggestionStatus.PENDING, reason="[auto-match] incomplete", matching_rule_version="5C-1.0")
    db_session.add(incomplete)
    await db_session.flush()
    ids = {"auto": str(s_auto.id), "review": str(s_review.id), "tie": [str(s_tie_a.id), str(s_tie_b.id)], "acc": str(s_acc.id),
           "rej": str(s_rej.id), "group": str(group.id), "inc": str(incomplete.id), "run": str(run.id)}
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "SUGGESTION", ids["acc"])).status_code == 200
    assert (await _decide(client, headers, "SUGGESTION", ids["rej"], "reject", REJECT)).status_code == 200
    return {**ids, "headers": headers}


async def test_reporting_classifications_agree_with_the_stage_5e_match_review_rules(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _classification_world(client, db_session, demo_group_and_entities, "cl", "9940000051")
    h = w["headers"]
    # 1. ground truth from the real Stage 5E endpoints
    awaiting = {p["id"] for p in await _proposals(client, h)}
    assert awaiting == {w["auto"], w["group"]}                                               # only these can be decided
    for sid in [w["review"], *w["tie"], w["inc"]]:                                           # the others are refused by Stage 5E itself
        assert (await _accept(client, h, "SUGGESTION", sid)).status_code == 400, sid
    summary = (await client.get(f"{MP}/summary", headers=h)).json()
    assert summary["pending_suggestions"] == 1 and summary["pending_groups"] == 1 and summary["pending_decisions"] == 2
    # 2. the reporting layer must agree with it
    [row] = [r for r in table_rows((await report(client, h, "run-summary")).json(), "runs") if r["id"] == w["run"]]
    assert row["pending_proposals"] == summary["pending_decisions"] == 2                     # actionable pending proposals
    assert row["rejected_proposals"] == 1
    # bank rows: auto, review, tie, acc, rej, grp, none (7).  accepted: acc.  proposed-only: auto + grp.  unmatched: review, tie, rej, none
    assert _buckets(row)["bank"] == (1, 2, 4, 7)
    # ledger rows: 10.  accepted: acc.  proposed-only: auto, grpA, grpB.  unmatched: review, tieA, tieB, rej, inc, none
    assert _buckets(row)["ledger"] == (1, 3, 6, 10)
    # non-actionable pending suggestions are reported separately: they are exceptions under investigation, not awaiting a decision
    assert row["pending_investigation"] == 4                                                  # review (1) + tie (2) + incomplete (1)
    assert row["investigating_bank"] == 2 and row["investigating_ledger"] == 4                # of the unmatched: review, tie / review, tieA, tieB, inc
    for side, (accepted, proposed, unmatched, eligible) in _buckets(row).items():
        assert accepted + proposed + unmatched == eligible, side


async def test_non_actionable_pending_suggestions_do_not_inflate_the_match_review_kpi(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _classification_world(client, db_session, demo_group_and_entities, "kp", "9940000061")
    h = w["headers"]
    overview = (await report(client, h, "overview")).json()
    assert kpis(overview)["pending"] == 2                                                     # the match-review KPI counts decidable proposals only
    perf = (await report(client, h, "matching-performance")).json()
    types = {r["type"]: r for r in table_rows(perf, "types")}
    assert (types["One-to-one"]["pending_proposals"], types["One-to-many"]["pending_proposals"]) == (1, 1)
    assert types["One-to-one"]["accepted_decisions"] == 1 and types["One-to-one"]["rejected_proposals"] == 1
    k = kpis(perf)
    assert (k["eligible"], k["accepted"], k["proposed"], k["unmatched"]) == (17, 2, 5, 10)
    buckets = {r["side"]: r for r in table_rows(perf, "buckets")}["Both (de-duplicated total)"]
    assert (buckets["total"], buckets["accepted"], buckets["proposed"], buckets["unmatched"]) == (17, 2, 5, 10)
    assert buckets["accepted"] + buckets["proposed"] + buckets["unmatched"] == buckets["total"]
    assert k["investigating"] == buckets["investigating"] == 6                                # reported separately, a subset of unmatched


async def test_pending_never_means_accepted_and_reporting_changes_no_status(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import ReconciliationMatchSuggestion

    w = await _classification_world(client, db_session, demo_group_and_entities, "pn", "9940000071")

    async def statuses():
        db_session.expire_all()
        return sorted((str(i), s.value) for i, s in (await db_session.execute(select(ReconciliationMatchSuggestion.id, ReconciliationMatchSuggestion.status))).all())

    before = await statuses()
    for name in ("run-summary", "matching-performance", "bank-accounts", "entity-dashboard", "overview"):
        assert (await report(client, w["headers"], name)).status_code == 200
    assert await statuses() == before
    assert {s for _, s in before} == {"PENDING", "ACCEPTED", "REJECTED"}
