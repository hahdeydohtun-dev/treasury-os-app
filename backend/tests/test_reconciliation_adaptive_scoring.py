"""
Stage 5G scoring, workflow integrity, versioning and reproducibility, against the real engine, the real Stage 5E decision API
and a real database. The guarantees under test: adaptive scoring is annotation only. It never changes a suggestion, its confidence,
its tier or whether it can be decided; humans still decide; history is immutable and reproducible; and a failure in adaptive code
can never fail a run.
"""
import datetime
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.audit import AuditEvent
from app.models.balance import BankBalance
from app.models.bank_statement import BankStatementTransaction
from app.models.reconciliation import (
    MatchSuggestionStatus,
    ReconciliationAcceptedClaim,
    ReconciliationMatchDecision,
    ReconciliationMatchSuggestion,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.models.reconciliation_adaptive import ReconciliationAdaptiveScore
from app.models.treasury_transaction import TreasuryTransaction
from app.services import reconciliation_adaptive_service as service
from app.services.reconciliation_adaptive_learning import aggregate, load_evidence, score_adaptively
from app.services.reconciliation_adaptive_settings import parse_settings
from app.services.reconciliation_adaptive_signals import PairView, pair_signals
from app.services.reconciliation_match_decision_service import suggestion_not_decidable_reason
from tests.reconciliation_adaptive_world import (
    REJECT,
    annotate,
    enabled_config,
    make_pair,
    scores,
    seed_history,
    target,
)
from tests.test_reconciliation_acceptance_conflicts import _accept, _group, _run, _suggestion
from tests.test_reconciliation_match_decisions import _decide, _proposals
from tests.test_reconciliation_matching_engine import _make_treasury_txn
from tests.test_reconciliation_open_items import _execute, _headers, _setup

S, A = ReconciliationMatchSuggestion, ReconciliationAdaptiveScore


async def _scene(client, db_session, demo, label, number, *, accepted=6, rejected=0, adaptive=None, confidence="80", reason=None, **seed):
    entity, account, user = await _setup(client, db_session, demo, label, number)
    await db_session.commit()
    headers = await _headers(client, user)
    history = await seed_history(client, db_session, headers, entity, account, accepted=accepted, rejected=rejected, **seed) if accepted + rejected else None
    cfg = await enabled_config(db_session, entity, account=account, adaptive=adaptive)
    kw = {"reason": reason} if reason else {}
    tgt = await target(db_session, entity, account, confidence=confidence, **kw)
    return {"entity": entity, "account": account, "user": user, "headers": headers, "history": history, "cfg": cfg, "tgt": tgt}


async def _fresh(db_session, model, pk):
    """Re-read one row from the database (refreshing the loaded object) without expiring anything else."""
    return (await db_session.execute(select(model).where(model.id == pk).execution_options(populate_existing=True))).scalar_one()


async def _state(db_session, suggestion):
    """Everything about a suggestion that adaptive scoring is forbidden to change."""
    s = await _fresh(db_session, S, suggestion.id)
    return (s.confidence, s.status, s.reason, s.match_type, s.matching_rule_version, s.bank_statement_transaction_id,
            s.treasury_transaction_id, s.updated_at, await suggestion_not_decidable_reason(db_session, s))


# ======================================================================================
# scoring
# ======================================================================================
async def test_a_learned_pattern_adjusts_the_recommendation_but_never_the_suggestion(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc1", "9980000011")
    suggestion = w["tgt"]["suggestions"][0]
    before = await _state(db_session, suggestion)
    queue_before = {p["id"] for p in await _proposals(client, w["headers"])}
    assert str(suggestion.id) in queue_before and before[-1] is None                       # actionable: decidable and in the queue

    result = await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    assert (result.enabled, result.annotated, result.contributing, result.fallback, result.failed) == (True, 1, 1, 0, False)
    [row] = await scores(db_session, w["tgt"]["run"].id)
    assert (row.deterministic_score, row.adaptive_contribution, row.recommendation_score) == (Decimal("80.00"), Decimal("7.50"), Decimal("87.50"))
    assert (row.score_band, row.support_level, row.fallback, row.fallback_reason) == ("AUTO", "MEDIUM", False, None)
    assert (row.accepted_examples, row.rejected_examples) == (6, 0)
    assert (row.learning_rule_version, row.base_rule_version, row.configuration_version, row.configuration_id) == ("5G-1.0", "5C-1.0", 1, w["cfg"].id)
    assert row.suggestion_id == suggestion.id and row.legal_entity_id == w["entity"].id and row.bank_account_id == w["account"].id
    assert [p["supported"] for p in row.evidence] == [True] * 4 and {p["family"] for p in row.evidence} == {
        "date_offset", "reference_format", "payee_pattern", "amount_relation"}

    assert await _state(db_session, suggestion) == before                                 # the suggestion is byte-for-byte unchanged
    assert {p["id"] for p in await _proposals(client, w["headers"])} == queue_before      # the review queue is unchanged


async def test_rejected_history_lowers_the_recommendation(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc2", "9980000021", accepted=1, rejected=6)
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [row] = await scores(db_session, w["tgt"]["run"].id)
    assert row.fallback is False and row.adaptive_contribution == Decimal("-5.56") and row.recommendation_score == Decimal("74.44")
    assert (row.accepted_examples, row.rejected_examples) == (1, 6) and row.score_band == "AUTO"


async def test_insufficient_history_falls_back_to_the_deterministic_score_and_says_so(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc3", "9980000031", accepted=3)
    before = await _state(db_session, w["tgt"]["suggestions"][0])
    result = await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    assert (result.annotated, result.contributing, result.fallback) == (1, 0, 1)
    [row] = await scores(db_session, w["tgt"]["run"].id)
    assert row.fallback and row.adaptive_contribution == 0 and row.recommendation_score == row.deterministic_score == Decimal("80.00")
    assert "enough evidence" in row.fallback_reason and (row.accepted_examples, row.rejected_examples) == (0, 0)
    assert any(not p["supported"] and "only 3" in p["note"] for p in row.evidence)             # shows WHY each pattern was not used
    assert await _state(db_session, w["tgt"]["suggestions"][0]) == before


async def test_the_configured_maximum_contribution_is_enforced(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc4", "9980000041", adaptive={"max_boost": 2})
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [row] = await scores(db_session, w["tgt"]["run"].id)
    assert row.adaptive_contribution == Decimal("1.50") and row.adaptive_contribution <= 2 and row.recommendation_score == Decimal("81.50")


async def test_one_anomalous_decision_cannot_dominate_a_pattern(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc5", "9980000051", accepted=6, rejected=1)
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [row] = await scores(db_session, w["tgt"]["run"].id)
    assert row.adaptive_contribution == Decimal("5.56") and (row.accepted_examples, row.rejected_examples) == (6, 1)
    assert row.adaptive_contribution < Decimal("7.50")                                       # dampened by the dissent, not flipped by it


async def test_ties_review_tier_and_placeholders_stay_non_decidable_whatever_the_evidence(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc6", "9980000061")
    run, entity, account = w["tgt"]["run"], w["entity"], w["account"]
    bank, ledger_a = await make_pair(db_session, entity, account, i=951)
    ledger_b = await _make_treasury_txn(db_session, entity_id=entity.id, account_id=account.id, event_date="2026-06-10", amount="1000",
                                        reference="INV-00952", counterparty="Acme Ltd")
    tie = "AMBIGUOUS: 2 candidates tied at score 80 - not auto-selected."
    tie_a, tie_b = await _suggestion(db_session, run, bank, ledger_a, confidence="80", reason=tie), await _suggestion(db_session, run, bank, ledger_b, confidence="80", reason=tie)
    bank2, ledger2 = await make_pair(db_session, entity, account, i=953)
    review = await _suggestion(db_session, run, bank2, ledger2, confidence="65", reason="[review] Exact amount; no usable reference match.")
    placeholder_bank, _unused = await make_pair(db_session, entity, account, i=954)
    placeholder = S(reconciliation_run_id=run.id, bank_statement_transaction_id=placeholder_bank.id, treasury_transaction_id=None,
                    match_type=None, confidence=None, status=MatchSuggestionStatus.PENDING, reason="No eligible candidate.", matching_rule_version="5C-1.0")
    db_session.add(placeholder)
    await db_session.commit()
    watched = [tie_a, tie_b, review, placeholder]
    before = [await _state(db_session, s) for s in watched]
    assert all(state[-1] is not None for state in before)                                      # none is decidable today
    queue_before = {p["id"] for p in await _proposals(client, w["headers"])}

    await annotate(db_session, run, w["cfg"], w["user"])
    rows = {r.suggestion_id: r for r in await scores(db_session, run.id)}
    assert {tie_a.id, tie_b.id, review.id} <= set(rows) and placeholder.id not in rows       # the placeholder has nothing to score
    assert [await _state(db_session, s) for s in watched] == before                           # still non-decidable, unchanged
    assert {p["id"] for p in await _proposals(client, w["headers"])} == queue_before
    assert not ({str(tie_a.id), str(tie_b.id), str(review.id), str(placeholder.id)} & queue_before)
    for s in (tie_a, tie_b):
        assert (await _decide(client, w["headers"], "SUGGESTION", str(s.id), "accept", "Verified.")).status_code == 400   # Stage 5E still refuses
    assert (await _decide(client, w["headers"], "SUGGESTION", str(review.id), "accept", "Verified.")).status_code == 400


async def test_tier_lock_on_real_rows_a_boost_cannot_lift_a_review_suggestion_across_the_threshold(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc7", "9980000071", confidence="69", reason="[review] Exact amount; date tolerance.")
    suggestion = w["tgt"]["suggestions"][0]
    before = await _state(db_session, suggestion)
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [row] = await scores(db_session, w["tgt"]["run"].id)
    assert row.deterministic_score == Decimal("69.00") and row.adaptive_contribution == Decimal("7.50")
    assert row.recommendation_score == Decimal("69.99") and row.score_band == "REVIEW"     # 69 + 7.50 = 76.50, locked below 70
    assert before[-1] is not None and await _state(db_session, suggestion) == before         # and it is still NOT decidable
    assert str(suggestion.id) not in {p["id"] for p in await _proposals(client, w["headers"])}


async def test_tier_lock_on_real_rows_a_penalty_cannot_drop_an_auto_suggestion_below_the_threshold(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc8", "9980000081", accepted=0, rejected=6, confidence="70")
    suggestion = w["tgt"]["suggestions"][0]
    before = await _state(db_session, suggestion)
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [row] = await scores(db_session, w["tgt"]["run"].id)
    assert row.deterministic_score == Decimal("70.00") and row.adaptive_contribution == Decimal("-7.50")
    assert row.recommendation_score == Decimal("70.00") and row.score_band == "AUTO"         # 70 - 7.50 = 62.50, locked at 70
    assert before[-1] is None and await _state(db_session, suggestion) == before             # still decidable: the penalty cannot hide it
    assert str(suggestion.id) in {p["id"] for p in await _proposals(client, w["headers"])}


async def test_only_complete_pending_suggestions_are_scored_and_groups_never_are(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "sc9", "9980000091")
    run, entity, account = w["tgt"]["run"], w["entity"], w["account"]
    gb, gl = await make_pair(db_session, entity, account, i=961)
    group = await _group(db_session, run, "ONE_TO_MANY", [gb], [gl])
    await db_session.commit()
    group_status = group.status
    await annotate(db_session, run, w["cfg"], w["user"])
    rows = await scores(db_session, run.id)
    assert len(rows) == 1 and rows[0].suggestion_id == w["tgt"]["suggestions"][0].id          # the group has no score and cannot have one
    assert (await _fresh(db_session, type(group), group.id)).status == group_status                # Stage 5D proposals continue through the unchanged path


# ======================================================================================
# reproducibility, history and versioning
# ======================================================================================
async def test_annotation_is_idempotent_and_every_score_can_be_recomputed_from_its_own_record(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "rp1", "9980000101", accepted=6, rejected=1)
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [first] = await scores(db_session, w["tgt"]["run"].id)
    snapshot = (first.id, first.created_at, first.deterministic_score, first.adaptive_contribution, first.recommendation_score, first.evidence)
    again = await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    assert again.annotated == 0                                                               # re-running annotates nothing new
    assert again.failed is False and again.error is None                                      # ...and cleanly: the skip is not left to a DB error
    failures = (await db_session.execute(select(AuditEvent).where(AuditEvent.action == "ADAPTIVE_ANNOTATE_FAILED"))).scalars().all()
    assert failures == []
    [second] = await scores(db_session, w["tgt"]["run"].id)
    assert (second.id, second.created_at, second.deterministic_score, second.adaptive_contribution, second.recommendation_score, second.evidence) == snapshot

    # reproduce it from nothing but the stored record: its cutoff, its settings snapshot and the authoritative decisions
    suggestion = await db_session.get(S, first.suggestion_id)
    bank = await db_session.get(BankStatementTransaction, suggestion.bank_statement_transaction_id)
    ledger = await db_session.get(TreasuryTransaction, suggestion.treasury_transaction_id)
    settings = parse_settings({"adaptive": first.settings_snapshot["settings"]}).settings
    evidence = await load_evidence(db_session, first.legal_entity_id, first.bank_account_id, first.evidence_cutoff, settings)
    signals = pair_signals(PairView(bank.transaction_date, ledger.event_date, bank.amount, ledger.transaction_amount,
                                    (bank.bank_reference, bank.external_transaction_id), (ledger.reference, ledger.external_reference),
                                    bank.narration, ledger.counterparty))
    redone = score_adaptively(suggestion.confidence, first.currency_code, first.direction, first.match_type, signals,
                              aggregate(evidence.rows, first.evidence_cutoff, settings), settings)
    assert (redone.contribution, redone.recommendation, redone.band, redone.support_level, redone.fallback) == (
        first.adaptive_contribution, first.recommendation_score, first.score_band, first.support_level, first.fallback)
    assert first.settings_snapshot["evidence_decisions_eligible"] == len(evidence.rows) == 7


async def test_new_decisions_change_only_future_runs_never_a_stored_score(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "rp2", "9980000111")
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [old] = await scores(db_session, w["tgt"]["run"].id)
    frozen = (old.id, old.adaptive_contribution, old.recommendation_score, old.evidence_cutoff, old.evidence, old.settings_snapshot, old.created_at)
    assert old.adaptive_contribution == Decimal("7.50")

    await seed_history(client, db_session, w["headers"], w["entity"], w["account"], accepted=6, start=40)       # six MORE accepted decisions
    tgt2 = await target(db_session, w["entity"], w["account"], start=990)
    await annotate(db_session, tgt2["run"], w["cfg"], w["user"])
    [new] = await scores(db_session, tgt2["run"].id)
    assert new.adaptive_contribution == Decimal("8.57") and new.accepted_examples == 12        # 10 * 12/14
    [still_old] = await scores(db_session, w["tgt"]["run"].id)
    assert (still_old.id, still_old.adaptive_contribution, still_old.recommendation_score, still_old.evidence_cutoff, still_old.evidence,
            still_old.settings_snapshot, still_old.created_at) == frozen                        # the earlier run's explanation did not move
    assert old.evidence_cutoff < new.evidence_cutoff


async def test_stored_explanations_are_immutable_and_scope_consistent_at_the_database_level(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "rp3", "9980000121")
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [row] = await scores(db_session, w["tgt"]["run"].id)
    row_id, run_id, entity_id = row.id, w["tgt"]["run"].id, w["entity"].id
    # scope isolation: a row can never be attributed to a different entity than its suggestion's run
    other_entity, other_account, _u = await _setup(client, db_session, demo_group_and_entities, "rp3b", "9980000122", entity_index=1)
    t2 = await target(db_session, w["entity"], w["account"], start=995)
    forged = ReconciliationAdaptiveScore(
        suggestion_id=t2["suggestions"][0].id, reconciliation_run_id=t2["run"].id, legal_entity_id=other_entity.id,
        bank_account_id=other_account.id, currency_code="NGN", direction="CREDIT", match_type="EXACT",
        deterministic_score=Decimal(80), adaptive_contribution=Decimal(0), recommendation_score=Decimal(80), score_band="AUTO",
        fallback=True, fallback_reason="x", support_level="NONE", accepted_examples=0, rejected_examples=0, learning_rule_version="5G-1.0",
        base_rule_version="5C-1.0", evidence_cutoff=datetime.datetime.now(datetime.UTC), settings_snapshot={}, evidence=[])
    other_entity_id = other_entity.id
    db_session.add(forged)
    with pytest.raises(DBAPIError, match="scope"):
        await db_session.flush()
    await db_session.rollback()                                            # (this expires every loaded object: only plain ids are used below)
    assert entity_id != other_entity_id
    # append-only: neither UPDATE nor DELETE is possible, however it is attempted
    for statement in (text("UPDATE reconciliation_adaptive_scores SET adaptive_contribution = 9 WHERE id = :id").bindparams(id=row_id),
                      text("UPDATE reconciliation_adaptive_scores SET fallback_reason = 'edited' WHERE id = :id").bindparams(id=row_id),
                      sa_delete(ReconciliationAdaptiveScore).where(ReconciliationAdaptiveScore.id == row_id)):   # a DELETE is refused too
        with pytest.raises(DBAPIError, match="immutable"):
            await db_session.execute(statement)
        await db_session.rollback()
    [unchanged] = await scores(db_session, run_id)
    assert unchanged.id == row_id and unchanged.adaptive_contribution == Decimal("7.50")


@pytest.mark.parametrize("override", [
    {"adaptive_contribution": Decimal(16)}, {"recommendation_score": Decimal(101)}, {"score_band": "MAYBE"}, {"support_level": "HUGE"},
    {"accepted_examples": -1}, {"fallback": True, "adaptive_contribution": Decimal("1.00")},
])
async def test_database_constraints_reject_impossible_explanations(client, db_session, demo_group_and_entities, cash_event_types, override):
    w = await _scene(client, db_session, demo_group_and_entities, "rp4", "9980000131")
    base = {
        "suggestion_id": w["tgt"]["suggestions"][0].id, "reconciliation_run_id": w["tgt"]["run"].id, "legal_entity_id": w["entity"].id,
        "bank_account_id": w["account"].id, "currency_code": "NGN", "direction": "CREDIT", "match_type": "EXACT", "deterministic_score": Decimal(80),
        "adaptive_contribution": Decimal(0), "recommendation_score": Decimal(80), "score_band": "AUTO", "fallback": False, "fallback_reason": None,
        "support_level": "NONE", "accepted_examples": 0, "rejected_examples": 0, "learning_rule_version": "5G-1.0", "base_rule_version": "5C-1.0",
        "evidence_cutoff": datetime.datetime.now(datetime.UTC), "settings_snapshot": {}, "evidence": []}
    db_session.add(ReconciliationAdaptiveScore(**{**base, **override}))
    with pytest.raises(DBAPIError):
        await db_session.flush()
    await db_session.rollback()


async def test_disabling_adaptive_matching_keeps_history_and_restores_deterministic_behaviour(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "dv1", "9980000141")
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    [old] = await scores(db_session, w["tgt"]["run"].id)
    old_values = (old.id, old.adaptive_contribution, old.recommendation_score, old.evidence)

    disabled = await enabled_config(db_session, w["entity"], account=w["account"], adaptive={"enabled": False}, version=2)
    t2 = await target(db_session, w["entity"], w["account"], start=870)
    before = [await _state(db_session, s) for s in t2["suggestions"]]
    result = await annotate(db_session, t2["run"], disabled, w["user"])
    assert (result.enabled, result.annotated) == (False, 0)
    assert await scores(db_session, t2["run"].id) == []                                         # nothing computed while disabled
    assert [await _state(db_session, s) for s in t2["suggestions"]] == before                    # purely deterministic
    [kept] = await scores(db_session, w["tgt"]["run"].id)
    assert (kept.id, kept.adaptive_contribution, kept.recommendation_score, kept.evidence) == old_values   # history is NOT erased
    actions = [e.action for e in (await db_session.execute(select(AuditEvent).where(AuditEvent.record_type == "ReconciliationRun"))).scalars()]
    assert "ADAPTIVE_ANNOTATE" in actions                                                       # the enabled run was audited; the disabled one wrote nothing


@pytest.mark.parametrize("adaptive", [None, {"enabled": False}, {"enabled": True, "scope": "group"}, {"enabled": "yes"}, {"enabled": True, "max_boost": "lots"}])
async def test_absent_disabled_or_unusable_settings_never_annotate(client, db_session, demo_group_and_entities, cash_event_types, adaptive):
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, "dv2", "9980000151")
    await db_session.commit()
    headers = await _headers(client, user)
    await seed_history(client, db_session, headers, entity, account, accepted=6)
    tgt = await target(db_session, entity, account)
    cfg = await enabled_config(db_session, entity, account=account)
    cfg.matching_rule_config = {} if adaptive is None else {"adaptive": adaptive}
    await db_session.commit()
    result = await annotate(db_session, tgt["run"], cfg, user)
    assert result.annotated == 0 and await scores(db_session, tgt["run"].id) == []
    assert (await annotate(db_session, tgt["run"], None, user)).annotated == 0                    # no configuration at all: deterministic only


# ======================================================================================
# workflow integrity: humans decide; Stage 5E rules and claim protection are untouched
# ======================================================================================
async def test_scored_suggestions_stay_pending_and_a_human_decision_still_goes_through_stage_5e(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "wf1", "9980000161")
    second = await target(db_session, w["entity"], w["account"], start=880)
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    await annotate(db_session, second["run"], w["cfg"], w["user"])
    decisions_before = (await db_session.execute(select(ReconciliationMatchDecision.id))).all()
    claims_before = (await db_session.execute(select(ReconciliationAcceptedClaim.id))).all()
    one, two = w["tgt"]["suggestions"][0], second["suggestions"][0]
    for s in (one, two):
        assert (await _fresh(db_session, S, s.id)).status == MatchSuggestionStatus.PENDING                   # annotation decided nothing

    assert (await _accept(client, w["headers"], "SUGGESTION", str(one.id))).status_code == 200              # explicit human acceptance
    assert (await _decide(client, w["headers"], "SUGGESTION", str(two.id), "reject", REJECT)).status_code == 200
    assert (await _fresh(db_session, S, one.id)).status == MatchSuggestionStatus.ACCEPTED
    assert (await _fresh(db_session, S, two.id)).status == MatchSuggestionStatus.REJECTED
    assert len((await db_session.execute(select(ReconciliationMatchDecision.id))).all()) == len(decisions_before) + 2
    assert len((await db_session.execute(select(ReconciliationAcceptedClaim.id))).all()) == len(claims_before) + 2   # bank + ledger of the accepted one
    scored = {r.suggestion_id: r for r in await scores(db_session)}
    assert scored[one.id].adaptive_contribution == Decimal("7.50") and scored[two.id].adaptive_contribution == Decimal("7.50")   # the stored explanation did not move


async def test_accepted_claim_uniqueness_still_blocks_a_second_acceptance_with_scores_present(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "wf2", "9980000171")
    first = w["tgt"]["suggestions"][0]
    run_b = await _run(db_session, w["entity"], w["account"])
    competing = await _suggestion(db_session, run_b, await db_session.get(BankStatementTransaction, first.bank_statement_transaction_id),
                                  await db_session.get(TreasuryTransaction, first.treasury_transaction_id), confidence="90")
    await db_session.commit()
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    await annotate(db_session, run_b, w["cfg"], w["user"])
    assert len(await scores(db_session)) == 2                                                   # both competing suggestions carry a score
    claims_before = len((await db_session.execute(select(ReconciliationAcceptedClaim.id))).all())
    assert (await _accept(client, w["headers"], "SUGGESTION", str(first.id))).status_code == 200
    conflict = await _accept(client, w["headers"], "SUGGESTION", str(competing.id))
    assert conflict.status_code == 409                                                          # the same bank row cannot be accepted twice
    assert (await _fresh(db_session, S, competing.id)).status == MatchSuggestionStatus.PENDING     # the loser stays pending: nothing decided for it
    assert len((await db_session.execute(select(ReconciliationAcceptedClaim.id))).all()) == claims_before + 2     # only the winner's bank + ledger claims


async def test_adaptive_scoring_has_no_financial_side_effects(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _scene(client, db_session, demo_group_and_entities, "wf3", "9980000181")

    async def snapshot():
        return ((await db_session.execute(select(TreasuryTransaction.id, TreasuryTransaction.transaction_amount, TreasuryTransaction.status,
                                                 TreasuryTransaction.updated_at).order_by(TreasuryTransaction.id))).all(),
                (await db_session.execute(select(BankStatementTransaction.id, BankStatementTransaction.amount, BankStatementTransaction.status,
                                                 BankStatementTransaction.updated_at).order_by(BankStatementTransaction.id))).all(),
                (await db_session.execute(select(BankBalance.id, BankBalance.updated_at).order_by(BankBalance.id))).all())

    before = await snapshot()
    assert before[0] and before[1]                                                              # not vacuous
    await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    assert await snapshot() == before


async def test_a_failure_in_adaptive_code_is_contained_audited_and_never_touches_the_suggestions(client, db_session, demo_group_and_entities, cash_event_types, monkeypatch):
    w = await _scene(client, db_session, demo_group_and_entities, "wf4", "9980000191")
    before = await _state(db_session, w["tgt"]["suggestions"][0])

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated adaptive failure")

    monkeypatch.setattr(service, "_annotate", boom)
    result = await annotate(db_session, w["tgt"]["run"], w["cfg"], w["user"])
    assert result.failed and result.error == "RuntimeError" and result.annotated == 0
    assert await scores(db_session, w["tgt"]["run"].id) == [] and await _state(db_session, w["tgt"]["suggestions"][0]) == before
    event = (await db_session.execute(select(AuditEvent).where(AuditEvent.action == "ADAPTIVE_ANNOTATE_FAILED"))).scalar_one()
    assert event.new_value["error"] == "RuntimeError" and event.record_id == str(w["tgt"]["run"].id)


# ======================================================================================
# end to end through the real engine and the real execute endpoint
# ======================================================================================
async def _engine_scene(client, db_session, demo, label, number):
    entity, account, user = await _setup(client, db_session, demo, label, number)
    await db_session.commit()
    headers = await _headers(client, user)
    # the real engine labels this pair TOLERANCE (its reference is not an exact match), and match type is an isolation dimension:
    # only TOLERANCE history can inform it
    await seed_history(client, db_session, headers, entity, account, accepted=6, match_type="TOLERANCE")
    saved = await client.put("/api/v1/reconciliation/adaptive/settings", headers=headers, json={
        "legal_entity_id": str(entity.id), "bank_account_id": str(account.id), "settings": {"enabled": True}, "reason": "pilot"})
    assert saved.status_code == 200, saved.text
    await make_pair(db_session, entity, account, i=900, bank_offset=0)                          # same-day pair the real engine will propose
    await db_session.commit()
    return entity, account, user, headers


async def test_the_real_engine_run_is_annotated_through_the_execution_hook(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, headers = await _engine_scene(client, db_session, demo_group_and_entities, "e2e1", "9980000201")
    run_id = await _execute(client, headers, entity, account)
    run = await _fresh(db_session, ReconciliationRun, uuid.UUID(run_id))
    assert run.status == ReconciliationRunStatus.COMPLETED and run.matching_rule_version == "5C-1.0"
    everything = (await db_session.execute(select(S).where(S.reconciliation_run_id == run.id))).scalars().all()
    [suggestion] = [x for x in everything if x.treasury_transaction_id is not None]                    # the other June bank rows get placeholders
    assert len(everything) > 1 and all(x.treasury_transaction_id is None for x in everything if x is not suggestion)
    assert suggestion.confidence == Decimal("65.00") and suggestion.reason.startswith("[review]") and suggestion.status == MatchSuggestionStatus.PENDING
    [row] = await scores(db_session, run.id)
    assert row.suggestion_id == suggestion.id and row.deterministic_score == suggestion.confidence      # the deterministic score is preserved exactly
    assert row.adaptive_contribution == Decimal("6.00"), (row.fallback_reason, {k: v for k, v in row.settings_snapshot.items() if k != "settings"}, [(p["family"], p["label"], p["accepted"], p["rejected"], p["note"]) for p in row.evidence])   # 3 supported families: (0.3 + 0.3 + 0.2) * 10 * 0.75
    assert row.recommendation_score == Decimal("69.99") and row.score_band == "REVIEW" and row.support_level == "MEDIUM"
    assert row.configuration_id == run.configuration_id and row.evidence_cutoff == run.started_at
    assert {p["family"] for p in row.evidence if p["supported"]} == {"reference_format", "payee_pattern", "amount_relation"}   # the +0d lag differs from history
    audit = (await db_session.execute(select(AuditEvent).where(AuditEvent.action == "ADAPTIVE_ANNOTATE", AuditEvent.record_id == str(run.id)))).scalar_one()
    assert audit.new_value["annotated"] == 1 and audit.new_value["contributing"] == 1 and audit.new_value["learning_rule_version"] == "5G-1.0"
    assert "NIP26" not in str(audit.new_value) and "INV-" not in str(audit.new_value)                  # no transaction details in the audit trail
    assert str(suggestion.id) not in {p["id"] for p in await _proposals(client, headers)}               # still a review-tier exception, not decidable


async def test_an_adaptive_failure_cannot_fail_a_real_reconciliation_run(client, db_session, demo_group_and_entities, cash_event_types, monkeypatch):
    entity, account, _user, headers = await _engine_scene(client, db_session, demo_group_and_entities, "e2e2", "9980000211")

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated adaptive failure")

    monkeypatch.setattr(service, "_annotate", boom)
    run_id = await _execute(client, headers, entity, account)                                          # asserts HTTP 200
    run = await _fresh(db_session, ReconciliationRun, uuid.UUID(run_id))
    assert run.status == ReconciliationRunStatus.COMPLETED and run.failure_reason is None
    assert (await db_session.execute(select(S).where(S.reconciliation_run_id == run.id))).scalars().all()      # deterministic output is intact
    assert await scores(db_session, run.id) == []
    assert (await db_session.execute(select(AuditEvent).where(AuditEvent.action == "ADAPTIVE_ANNOTATE_FAILED"))).scalar_one()
