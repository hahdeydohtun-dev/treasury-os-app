"""
Stage 5F final edge case: incomplete PENDING suggestions must be classified symmetrically.

Evidence (see docs/STAGE_5F_RECONCILIATION_REPORTS.md): both reference columns of a suggestion are nullable with no CHECK.
The Stage 5C engine always anchors a suggestion on a bank row; its "no candidate found" placeholder is the shape
bank-present / ledger-NULL. The inverse (ledger-present / bank-NULL) is representable but never written by the engine.
Stage 5E treats BOTH as "not a proposed match" (same refusal text) and its exception derivation ignores both (it needs both
references), so each leaves its transaction as a plain BANK_ONLY / LEDGER_ONLY exception. Reporting therefore treats both
shapes the same way: not actionable, the row is unmatched and counted as under investigation; a suggestion that references
NO transaction at all (both NULL) is an orphan that no report counts.
"""
from sqlalchemy import func, select

from tests.reconciliation_reports_world import JUNE, kpis, report, table_rows
from tests.test_reconciliation_acceptance_conflicts import (
    _accept,
    _bank,
    _ledger,
    _run,
    _suggestion,
)
from tests.test_reconciliation_match_decisions import MP, _decide, _proposals
from tests.test_reconciliation_matching_engine import _make_bank_statement_txn, _make_treasury_txn
from tests.test_reconciliation_open_items import _execute, _headers, _setup

DAY = "2026-06-12"
REJECT = "Customer confirmed this receipt belongs to a different invoice."


async def _incomplete(db_session, run, *, bank=None, ledger=None):
    """A PENDING suggestion with a missing reference, built from the real model (the shape the engine may or may not write)."""
    from app.models.reconciliation import MatchSuggestionStatus, ReconciliationMatchSuggestion

    s = ReconciliationMatchSuggestion(
        reconciliation_run_id=run.id, bank_statement_transaction_id=bank.id if bank else None,
        treasury_transaction_id=ledger.id if ledger else None, match_type=None, confidence=None,
        status=MatchSuggestionStatus.PENDING, reason="incomplete candidate", matching_rule_version="5C-1.0")
    db_session.add(s)
    await db_session.flush()
    return s


def _row(result, run_id):
    return next(r for r in table_rows(result, "runs") if r["id"] == str(run_id))


def _b(row):
    return {"bank": (row["accepted_bank"], row["proposed_bank"], row["unmatched_bank"], row["bank_in_scope"], row["investigating_bank"]),
            "ledger": (row["accepted_ledger"], row["proposed_ledger"], row["unmatched_ledger"], row["ledger_in_scope"], row["investigating_ledger"])}


# ======================================================================================
# evidence from the REAL engine
# ======================================================================================
async def test_real_engine_writes_only_bank_anchored_suggestions_and_the_placeholder_has_a_null_ledger_reference(
        client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import MatchSuggestionStatus, ReconciliationMatchSuggestion

    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "eng", "9950000011")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, bank_reference="PAY-12345", narration="Supplier ABC")
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, direction="INFLOW", reference="PAY-12345", narration="Supplier ABC")
    await _make_bank_statement_txn(db_session, entity_id=entity.id, account_id=account.id, amount="77777", bank_reference="NO-CANDIDATE-1", row_number=2)
    await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, amount="5555", direction="INFLOW", reference="LEDGER-ONLY")
    await db_session.commit()
    headers = await _headers(client, user)
    run_id = await _execute(client, headers, entity, account)

    db_session.expire_all()
    rows = (await db_session.execute(select(ReconciliationMatchSuggestion.bank_statement_transaction_id, ReconciliationMatchSuggestion.treasury_transaction_id,
                                            ReconciliationMatchSuggestion.status, ReconciliationMatchSuggestion.confidence))).all()
    shapes = sorted((b is not None, t is not None) for b, t, _s, _c in rows)
    assert shapes == [(True, False), (True, True)]                    # one real candidate, one placeholder
    assert not any(b is None for b, _t, _s, _c in rows)               # the engine NEVER writes a ledger-only (bank NULL) suggestion
    [placeholder] = [r for r in rows if r[1] is None]
    assert placeholder[2] == MatchSuggestionStatus.PENDING and placeholder[3] is None

    # the unlinked ledger row has no suggestion at all, and the report treats the placeholder's bank row as an exception under investigation
    row = _row((await report(client, headers, "run-summary")).json(), run_id)
    assert _b(row)["bank"] == (0, 1, 1, 2, 1)       # PAY proposed (actionable); NO-CANDIDATE unmatched AND under investigation
    assert _b(row)["ledger"] == (0, 1, 1, 2, 0)     # PAY proposed; LEDGER-ONLY unmatched, no suggestion, so not 'investigating'
    assert (row["pending_proposals"], row["pending_investigation"]) == (1, 1)


# ======================================================================================
# every reference shape, in one run
# ======================================================================================
async def _shapes_world(client, db_session, demo, label, number):
    entity, account, user = await _setup(client, db_session, demo, label, number)
    run = await _run(db_session, entity, account, *JUNE)
    bank = {k: await _bank(db_session, entity, account, f"{label}-B{k}", amount="100", row=2 + i, txn_date=DAY)
            for i, k in enumerate(("auto", "ph", "acc", "rej", "none"))}
    led = {k: await _ledger(db_session, entity, account, f"{label}-L{k}", amount="100", event_date=DAY) for k in ("auto", "A", "acc", "rej", "none")}
    s_auto = await _suggestion(db_session, run, bank["auto"], led["auto"])                 # both present, actionable
    placeholder = await _incomplete(db_session, run, bank=bank["ph"])                      # bank present, ledger MISSING  (the 5C placeholder)
    shape_a = await _incomplete(db_session, run, ledger=led["A"])                          # ledger present, bank MISSING
    orphan = await _incomplete(db_session, run)                                            # both MISSING
    s_acc = await _suggestion(db_session, run, bank["acc"], led["acc"])                    # will be accepted
    s_rej = await _suggestion(db_session, run, bank["rej"], led["rej"])                    # will be rejected
    ids = {"run": run.id, "auto": str(s_auto.id), "ph": str(placeholder.id), "a": str(shape_a.id), "orphan": str(orphan.id), "acc": str(s_acc.id), "rej": str(s_rej.id)}
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "SUGGESTION", ids["acc"])).status_code == 200
    assert (await _decide(client, headers, "SUGGESTION", ids["rej"], "reject", REJECT)).status_code == 200
    return {**ids, "headers": headers}


async def test_both_incomplete_shapes_are_classified_symmetrically_and_the_orphan_is_ignored(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _shapes_world(client, db_session, demo_group_and_entities, "sh", "9950000021")
    row = _row((await report(client, w["headers"], "run-summary")).json(), w["run"])
    # bank rows: auto, ph, acc, rej, none (5).  accepted: acc.  proposed: auto.  unmatched: ph, rej, none.  investigating: ph (the placeholder)
    assert _b(row)["bank"] == (1, 1, 3, 5, 1)
    # ledger rows: auto, A, acc, rej, none (5).  accepted: acc.  proposed: auto.  unmatched: A, rej, none.  investigating: A (the inverse shape)
    assert _b(row)["ledger"] == (1, 1, 3, 5, 1)
    assert row["pending_proposals"] == 1 and row["rejected_proposals"] == 1
    assert row["pending_investigation"] == 2                       # placeholder + ledger-only; the both-NULL orphan references nothing
    for side, (accepted, proposed, unmatched, eligible, investigating) in _b(row).items():
        assert accepted + proposed + unmatched == eligible, side
        assert investigating <= unmatched, side                    # a subset of unmatched, never a fourth bucket


async def test_accepted_and_rejected_counts_match_the_real_claims_and_decisions(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import ReconciliationAcceptedClaim, ReconciliationMatchDecision

    w = await _shapes_world(client, db_session, demo_group_and_entities, "gt", "9950000031")
    row = _row((await report(client, w["headers"], "run-summary")).json(), w["run"])
    db_session.expire_all()
    bank_claims = (await db_session.execute(select(func.count()).select_from(ReconciliationAcceptedClaim).join(
        ReconciliationMatchDecision, ReconciliationMatchDecision.id == ReconciliationAcceptedClaim.decision_id).where(
        ReconciliationMatchDecision.reconciliation_run_id == w["run"], ReconciliationAcceptedClaim.bank_statement_transaction_id.is_not(None)))).scalar_one()
    ledger_claims = (await db_session.execute(select(func.count()).select_from(ReconciliationAcceptedClaim).join(
        ReconciliationMatchDecision, ReconciliationMatchDecision.id == ReconciliationAcceptedClaim.decision_id).where(
        ReconciliationMatchDecision.reconciliation_run_id == w["run"], ReconciliationAcceptedClaim.treasury_transaction_id.is_not(None)))).scalar_one()
    assert (row["accepted_bank"], row["accepted_ledger"]) == (bank_claims, ledger_claims) == (1, 1)    # accepted = real claims; nothing else
    rejected = (await db_session.execute(select(func.count()).select_from(ReconciliationMatchDecision).where(
        ReconciliationMatchDecision.reconciliation_run_id == w["run"], ReconciliationMatchDecision.decision == "REJECTED"))).scalar_one()
    assert row["rejected_proposals"] == rejected == 1


async def test_report_and_match_review_queue_agree_that_incomplete_shapes_are_not_actionable(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _shapes_world(client, db_session, demo_group_and_entities, "mr", "9950000041")
    h = w["headers"]
    assert {p["id"] for p in await _proposals(client, h)} == {w["auto"]}                    # only the complete auto-tier suggestion is decidable
    for key in ("ph", "a", "orphan"):
        assert (await _accept(client, h, "SUGGESTION", w[key])).status_code == 400, key        # Stage 5E refuses all three incomplete shapes
        assert (await _decide(client, h, "SUGGESTION", w[key], "reject", REJECT)).status_code == 400, key
    summary = (await client.get(f"{MP}/summary", headers=h)).json()
    row = _row((await report(client, h, "run-summary")).json(), w["run"])
    assert row["pending_proposals"] == summary["pending_decisions"] == 1
    assert kpis((await report(client, h, "overview")).json())["pending"] == 1                  # the overview KPI counts decidable proposals only
    perf = (await report(client, h, "matching-performance")).json()
    k = kpis(perf)
    assert (k["eligible"], k["accepted"], k["proposed"], k["unmatched"], k["investigating"]) == (10, 2, 2, 6, 2)
    assert k["accepted"] + k["proposed"] + k["unmatched"] == k["eligible"]


# ======================================================================================
# two runs, same transaction population, different decisions: nothing is inherited
# ======================================================================================
async def test_incomplete_shapes_stay_run_scoped_and_do_not_inherit_another_runs_decisions(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "rs", "9950000051")
    run_a = await _run(db_session, entity, account, "2026-06-01", "2026-06-30")
    run_b = await _run(db_session, entity, account, "2026-06-10", "2026-07-15")            # overlaps A; both cover 2026-06-12
    bank = {k: await _bank(db_session, entity, account, f"rs-B{k}", amount="100", row=2 + i, txn_date=DAY) for i, k in enumerate((1, 2, 3))}
    led = {k: await _ledger(db_session, entity, account, f"rs-L{k}", amount="100", event_date=DAY) for k in (1, 2, 3)}
    await _incomplete(db_session, run_a, bank=bank[1])                                      # Run A: placeholder on B1 ...
    await _incomplete(db_session, run_a, ledger=led[2])                                     # ... and a ledger-only suggestion on L2
    s_acc = await _suggestion(db_session, run_b, bank[1], led[1])                           # Run B: B1 accepted with L1 ...
    s_rej = await _suggestion(db_session, run_b, bank[2], led[2])                           # ... and (B2, L2) rejected
    ids = {"a": str(run_a.id), "b": str(run_b.id), "acc": str(s_acc.id), "rej": str(s_rej.id)}
    await db_session.commit()
    headers = await _headers(client, user)
    assert (await _accept(client, headers, "SUGGESTION", ids["acc"])).status_code == 200
    assert (await _decide(client, headers, "SUGGESTION", ids["rej"], "reject", REJECT)).status_code == 200
    result = (await report(client, headers, "run-summary")).json()
    a, b = _row(result, ids["a"]), _row(result, ids["b"])
    # Run A made no decisions: it must not inherit B1/L1 as accepted, and keeps its own two incomplete suggestions under investigation
    assert _b(a) == {"bank": (0, 0, 3, 3, 1), "ledger": (0, 0, 3, 3, 1)}
    assert (a["pending_proposals"], a["rejected_proposals"], a["pending_investigation"]) == (0, 0, 2)
    # Run B accepted (B1, L1) and rejected (B2, L2); Run A's incomplete suggestions are not inherited, so nothing is under investigation
    assert _b(b) == {"bank": (1, 0, 2, 3, 0), "ledger": (1, 0, 2, 3, 0)}
    assert (b["pending_proposals"], b["rejected_proposals"], b["pending_investigation"]) == (0, 1, 0)
    for row in (a, b):
        for side, (accepted, proposed, unmatched, eligible, _inv) in _b(row).items():
            assert accepted + proposed + unmatched == eligible, (row["reference"], side)
