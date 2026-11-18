"""Stage 5G administration and explanation endpoints: settings, RBAC, versioning, audit, health, scoped explanations, queue ordering."""
import json
from decimal import Decimal

from sqlalchemy import func, select, update

from app.models.audit import AuditEvent
from app.models.bank_statement import BankStatementTransaction, BankStatementTransactionStatus
from app.models.rbac import TreasuryAction
from app.models.reconciliation import (
    ReconciliationConfiguration,
    ReconciliationMatchDecision,
    ReconciliationMatchSuggestion,
)
from app.services.reconciliation_service import resolve_effective_configuration
from tests.reconciliation_adaptive_world import (
    annotate,
    enabled_config,
    make_pair,
    scores,
    seed_history,
    target,
)
from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_acceptance_conflicts import _suggestion
from tests.test_reconciliation_match_decisions import MP, _proposals
from tests.test_reconciliation_open_items import _headers, _limited_user, _setup

ADAPTIVE = "/api/v1/reconciliation/adaptive"
CONFIGS = "/api/v1/reconciliation/configurations"
Cfg, D = ReconciliationConfiguration, ReconciliationMatchDecision


async def _admin(client, db_session, demo, label, number, **kw):
    entity, account, user = await _setup(client, db_session, demo, label, number, **kw)
    await db_session.commit()
    return entity, account, user, await _headers(client, user)


async def _put(client, headers, entity, account, settings, **extra):
    return await client.put(f"{ADAPTIVE}/settings", headers=headers, json={
        "legal_entity_id": str(entity.id), "bank_account_id": str(account.id) if account else None, "settings": settings, **extra})


async def _config_count(db_session):
    return (await db_session.execute(select(func.count()).select_from(Cfg))).scalar_one()


# ======================================================================================
# settings: defaults, versioning and audit
# ======================================================================================
async def test_settings_default_to_disabled_and_state_the_v1_scope_and_the_human_approval_rule(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _u, h = await _admin(client, db_session, demo_group_and_entities, "ap1", "9990000011")
    body = (await client.get(f"{ADAPTIVE}/settings", params={"legal_entity_id": str(entity.id), "bank_account_id": str(account.id)}, headers=h)).json()
    assert body["settings"]["enabled"] is False and body["disabled_by_default"] is True and body["automatic_acceptance"] is False
    assert body["configuration_id"] is None and body["warnings"] == [] and body["settings_unusable"] is False
    assert body["learning_rule_version"] == "5G-1.0" and body["base_rule_version"] == "5C-1.0"
    assert "one-to-one" in body["scope_note"] and "not learned from" in body["scope_note"] and "person must accept or reject" in body["human_review_required"]
    assert body["bounds"]["max_boost"] == ["0", "15"] and body["defaults"]["min_evidence"] == 5


async def test_a_settings_change_is_a_new_configuration_version_and_is_audited(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, h = await _admin(client, db_session, demo_group_and_entities, "ap2", "9990000021")
    first = await _put(client, h, entity, account, {"enabled": True, "min_evidence": 6}, reason="pilot on the operating account")
    assert first.status_code == 200, first.text
    assert (first.json()["configuration_version"], first.json()["settings"]["enabled"], first.json()["settings"]["min_evidence"]) == (1, True, 6)
    second = await _put(client, h, entity, account, {"enabled": True, "min_evidence": 8, "max_boost": 6})
    assert second.json()["configuration_version"] == 2
    rows = (await db_session.execute(select(Cfg).where(Cfg.legal_entity_id == entity.id).order_by(Cfg.version)
                                     .execution_options(populate_existing=True))).scalars().all()
    assert [(c.version, c.is_current) for c in rows] == [(1, False), (2, True)] and rows[0].superseded_by_id == rows[1].id
    assert rows[0].matching_rule_config["adaptive"]["min_evidence"] == 6                       # the old version is untouched, so old runs stay reproducible
    events = (await db_session.execute(select(AuditEvent).where(AuditEvent.action == "ADAPTIVE_CONFIGURE").order_by(AuditEvent.created_at))).scalars().all()
    assert len(events) == 2 and all(e.user_id == user.test_id and e.module == "BANK_RECONCILIATION" for e in events)
    assert events[0].reason == "pilot on the operating account" and events[0].previous_value["adaptive"] is None
    assert events[1].previous_value["adaptive"]["min_evidence"] == 6 and events[1].new_value["adaptive"]["min_evidence"] == 8
    assert events[1].new_value["configuration_version"] == 2 and events[1].record_id == str(rows[1].id)


async def test_disabling_creates_a_new_version_and_keeps_every_earlier_explanation(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, h = await _admin(client, db_session, demo_group_and_entities, "ap3", "9990000031")
    await seed_history(client, db_session, h, entity, account, accepted=6)
    assert (await _put(client, h, entity, account, {"enabled": True})).status_code == 200
    cfg = await resolve_effective_configuration(db_session, entity.id, account.id, "NGN")
    tgt = await target(db_session, entity, account)
    await annotate(db_session, tgt["run"], cfg, user)
    [before] = await scores(db_session, tgt["run"].id)
    kept = (before.id, before.adaptive_contribution, before.evidence, before.configuration_version)
    off = await _put(client, h, entity, account, {"enabled": False})
    assert off.json()["settings"]["enabled"] is False and off.json()["configuration_version"] == 2
    [after] = await scores(db_session, tgt["run"].id)
    assert (after.id, after.adaptive_contribution, after.evidence, after.configuration_version) == kept       # disabling erased nothing
    fetched = (await client.get(f"{ADAPTIVE}/explanations", params={"suggestion_id": str(tgt["suggestions"][0].id)}, headers=h)).json()
    assert fetched["explanations"][str(tgt["suggestions"][0].id)]["adaptive_contribution"] == "7.50"          # still readable after disabling


async def test_enabling_adaptive_matching_never_changes_the_existing_matching_settings(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _u, h = await _admin(client, db_session, demo_group_and_entities, "ap4", "9990000041")
    other = await _make_account(db_session, entity.id, (await _make_bank(db_session)).id, account_number="4433221100")
    await db_session.commit()
    broad = await client.post(CONFIGS, headers=h, json={
        "legal_entity_id": str(entity.id), "amount_tolerance_pct": "0.5", "date_tolerance_days": 2, "high_value_threshold": "5000000",
        "duplicate_policy": "STRICT", "matching_rule_config": {"fx_rate_type": "SPOT", "fx_max_staleness_days": 3}, "effective_from": "2026-01-01"})
    assert broad.status_code == 201, broad.text
    assert (await _put(client, h, entity, account, {"enabled": True})).status_code == 200
    forked = await resolve_effective_configuration(db_session, entity.id, account.id, "NGN")
    assert forked.bank_account_id == account.id and forked.id != uuid_of(broad)
    assert (forked.amount_tolerance_pct, forked.date_tolerance_days, forked.high_value_threshold, forked.duplicate_policy) == (
        Decimal("0.500"), 2, Decimal("5000000.00"), "STRICT")                                  # every tolerance carried over: matching is unchanged
    assert forked.matching_rule_config["fx_rate_type"] == "SPOT" and forked.matching_rule_config["fx_max_staleness_days"] == 3
    assert forked.matching_rule_config["adaptive"]["enabled"] is True
    entity_wide = await db_session.get(Cfg, uuid_of(broad), populate_existing=True)
    assert entity_wide.is_current and entity_wide.version == 1 and "adaptive" not in entity_wide.matching_rule_config   # the broad config is untouched
    sibling = await client.get(f"{ADAPTIVE}/settings", params={"legal_entity_id": str(entity.id), "bank_account_id": str(other.id)}, headers=h)
    assert sibling.json()["settings"]["enabled"] is False                                       # other accounts of the entity are unaffected


def uuid_of(response):
    import uuid
    return uuid.UUID(response.json()["id"])


async def test_invalid_settings_are_rejected_and_create_nothing(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _u, h = await _admin(client, db_session, demo_group_and_entities, "ap5", "9990000051")
    before = await _config_count(db_session)
    for settings, fragment in (({"max_boost": 50}, "max_boost must be between"), ({"scope": "group"}, "only account-level"),
                               ({"enabled": "yes"}, "true or false"), ({"min_evidence": 1}, "min_evidence must be between"),
                               ({"nonsense": 1}, "unknown setting"), ({"min_support_level": "ULTRA"}, "min_support_level")):
        resp = await _put(client, h, entity, account, settings)
        assert resp.status_code == 422 and any(fragment in p for p in resp.json()["detail"]), settings
    assert await _config_count(db_session) == before


async def test_settings_that_bypass_the_endpoint_are_still_clamped_or_disabled_at_run_time(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _u, h = await _admin(client, db_session, demo_group_and_entities, "ap6", "9990000061")
    await client.post(CONFIGS, headers=h, json={"legal_entity_id": str(entity.id), "bank_account_id": str(account.id), "effective_from": "2026-01-01",
                                                "matching_rule_config": {"adaptive": {"enabled": True, "max_boost": 999}}})
    body = (await client.get(f"{ADAPTIVE}/settings", params={"legal_entity_id": str(entity.id), "bank_account_id": str(account.id)}, headers=h)).json()
    assert body["settings"]["enabled"] is True and body["settings"]["max_boost"] == "15" and any("clamped" in w for w in body["warnings"])
    await client.post(CONFIGS, headers=h, json={"legal_entity_id": str(entity.id), "bank_account_id": str(account.id), "effective_from": "2026-01-02",
                                                "matching_rule_config": {"adaptive": {"enabled": True, "scope": "group"}}})
    unusable = (await client.get(f"{ADAPTIVE}/settings", params={"legal_entity_id": str(entity.id), "bank_account_id": str(account.id)}, headers=h)).json()
    assert unusable["settings_unusable"] is True and unusable["settings"]["enabled"] is False  # fails safe to deterministic matching


# ======================================================================================
# RBAC and entity isolation
# ======================================================================================
async def test_only_configure_permission_can_change_settings_and_other_entities_are_off_limits(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _u, _h = await _admin(client, db_session, demo_group_and_entities, "ap7", "9990000071")
    other_entity, _other_account, _ou, other_h = await _admin(client, db_session, demo_group_and_entities, "ap7b", "9990000072", entity_index=1)
    viewer = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW], "ap7-viewer")
    nobody = await _limited_user(db_session, entity.id, [TreasuryAction.COMMENT], "ap7-nobody")
    await db_session.commit()
    vh, nh = await _headers(client, viewer), await _headers(client, nobody)
    q = {"legal_entity_id": str(entity.id), "bank_account_id": str(account.id)}
    before = await _config_count(db_session)
    assert (await client.get(f"{ADAPTIVE}/settings", params=q, headers=vh)).status_code == 200            # VIEW may read
    assert (await _put(client, vh, entity, account, {"enabled": True})).status_code == 403               # but not change
    assert (await client.get(f"{ADAPTIVE}/settings", params=q, headers=nh)).status_code == 403
    assert (await client.get(f"{ADAPTIVE}/health", params=q, headers=nh)).status_code == 403
    assert (await client.get(f"{ADAPTIVE}/settings", params=q, headers=other_h)).status_code == 403      # entity B's admin cannot read A
    assert (await _put(client, other_h, entity, account, {"enabled": True})).status_code == 403          # or change A
    assert (await client.get(f"{ADAPTIVE}/health", params=q, headers=other_h)).status_code == 403
    assert await _config_count(db_session) == before                                                      # nothing was created by a refused request
    mixed = await client.get(f"{ADAPTIVE}/settings", params={"legal_entity_id": str(other_entity.id), "bank_account_id": str(account.id)}, headers=other_h)
    assert mixed.status_code == 404                                                                       # an account that is not the entity's own
    assert (await client.get(f"{ADAPTIVE}/settings", params={"legal_entity_id": str(entity.id)})).status_code in (401, 403)


# ======================================================================================
# learning health
# ======================================================================================
async def test_learning_health_reports_evidence_exclusions_and_scored_suggestions_for_one_account(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, h = await _admin(client, db_session, demo_group_and_entities, "ap8", "9990000081")
    hist = await seed_history(client, db_session, h, entity, account, accepted=5, rejected=1)
    suggestion = await db_session.get(ReconciliationMatchSuggestion, hist["suggestion_ids"][0])
    await db_session.execute(update(BankStatementTransaction).where(BankStatementTransaction.id == suggestion.bank_statement_transaction_id)
                             .values(status=BankStatementTransactionStatus.REVERSED))
    await db_session.commit()
    cfg = await enabled_config(db_session, entity, account=account)
    tgt = await target(db_session, entity, account)
    await annotate(db_session, tgt["run"], cfg, user)
    body = (await client.get(f"{ADAPTIVE}/health", params={"legal_entity_id": str(entity.id), "bank_account_id": str(account.id)}, headers=h)).json()
    assert body["enabled"] is True and body["evidence"]["decisions_scanned"] == 6 and body["evidence"]["eligible"] == 5
    assert body["evidence"]["excluded_by_reason"] == {"BANK_TRANSACTION_REVERSED": 1}                    # the reversed row dropped out, visibly
    assert body["evidence"]["eligible_accepted"] + body["evidence"]["eligible_rejected"] == 5
    assert body["scored_suggestions"]["total"] == 1 and body["scored_suggestions"]["last_annotated_at"] is not None
    assert set(body["patterns_by_family"]) == {"date_offset", "reference_format", "payee_pattern", "amount_relation"}
    assert "one-to-one" in body["scope_note"] and "decision_ids" not in json.dumps(body) and "NIP26" not in json.dumps(body)



# ======================================================================================
# explanations: scoped, minimal and honest about what they are
# ======================================================================================
async def test_explanations_are_scoped_minimal_and_never_expose_another_entitys_data(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, h = await _admin(client, db_session, demo_group_and_entities, "ap9", "9990000091")
    _oe, _oa, _ou, other_h = await _admin(client, db_session, demo_group_and_entities, "ap9b", "9990000092", entity_index=1)
    viewer = await _limited_user(db_session, entity.id, [TreasuryAction.VIEW], "ap9-viewer")
    await db_session.commit()
    vh = await _headers(client, viewer)
    await seed_history(client, db_session, h, entity, account, accepted=6)
    cfg = await enabled_config(db_session, entity, account=account)
    tgt = await target(db_session, entity, account, count=2)
    await annotate(db_session, tgt["run"], cfg, user)
    scored, plain = tgt["suggestions"][0], tgt["suggestions"][1]
    rows = await scores(db_session, tgt["run"].id)
    ids = [("suggestion_id", str(s.id)) for s in (scored, plain)]
    body = (await client.get(f"{ADAPTIVE}/explanations", params=ids, headers=h)).json()
    assert set(body["explanations"]) == {str(r.suggestion_id) for r in rows} and body["without_explanation"] == []
    payload = body["explanations"][str(scored.id)]
    assert (payload["deterministic_score"], payload["adaptive_contribution"], payload["recommendation_score"]) == ("80.00", "7.50", "87.50")
    assert payload["human_review_required"] is True and payload["fallback"] is False and payload["learning_rule_version"] == "5G-1.0"
    assert payload["scope"] == {"level": "account", "bank_account_id": str(account.id), "currency_code": "NGN", "direction": "CREDIT", "match_type": "EXACT"}
    assert "suggestion only" in payload["explanation"] and "unchanged" in payload["does_not_change"]
    text_dump = json.dumps(body)
    assert "decision_ids" not in text_dump and "NIP26" not in text_dump and "INV-" not in text_dump       # no references, no decision ids by default
    unknown = "00000000-0000-0000-0000-00000000dead"
    mixed = (await client.get(f"{ADAPTIVE}/explanations", params=ids + [("suggestion_id", unknown)], headers=h)).json()
    assert unknown not in json.dumps(mixed)                                                               # a nonexistent id is simply absent
    foreign = (await client.get(f"{ADAPTIVE}/explanations", params=ids, headers=other_h)).json()          # entity B asking for A's ids
    assert foreign["explanations"] == {} and foreign["without_explanation"] == []                         # indistinguishable from nonexistent
    # decision references need CONFIGURE and are traceable to real decisions
    assert (await client.get(f"{ADAPTIVE}/explanations", params=ids + [("include_decision_ids", "true")], headers=vh)).status_code == 403
    traced = (await client.get(f"{ADAPTIVE}/explanations", params=ids + [("include_decision_ids", "true")], headers=h)).json()
    real = {str(i) for (i,) in (await db_session.execute(select(D.id))).all()}
    cited = {d for p in traced["explanations"][str(scored.id)]["patterns"] for d in p["decision_ids"]}
    assert cited and cited <= real and len(cited) == 6


async def test_a_suggestion_scored_before_or_while_disabled_is_reported_without_an_explanation(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, _user, h = await _admin(client, db_session, demo_group_and_entities, "ap10", "9990000101")
    tgt = await target(db_session, entity, account)
    body = (await client.get(f"{ADAPTIVE}/explanations", params=[("suggestion_id", str(tgt["suggestions"][0].id))], headers=h)).json()
    assert body["explanations"] == {} and body["without_explanation"] == [str(tgt["suggestions"][0].id)]
    assert "disabled" in body["note"] and "person must accept or reject" in body["note"]
    assert (await client.get(f"{ADAPTIVE}/explanations", headers=h)).status_code == 422                 # at least one id is required


# ======================================================================================
# the review queue: ordering is opt-in and changes nothing else
# ======================================================================================
async def test_recommendation_ordering_is_opt_in_and_the_default_order_and_membership_are_unchanged(client, db_session, demo_group_and_entities, cash_event_types):
    entity, account, user, h = await _admin(client, db_session, demo_group_and_entities, "ap11", "9990000111")
    await seed_history(client, db_session, h, entity, account, accepted=6)
    cfg = await enabled_config(db_session, entity, account=account)
    run = (await target(db_session, entity, account, count=0))["run"]
    # X: lower deterministic score (75) but follows the learned pattern;  Y: higher deterministic score (80) with no learned pattern
    bx, lx = await make_pair(db_session, entity, account, i=701)
    by, ly = await make_pair(db_session, entity, account, i=702, bank_offset=4, payee="Other Co", narration="POS purchase at shop", bank_ref="ZQ-777", ledger_ref="OTH/702")
    sx, sy = await _suggestion(db_session, run, bx, lx, confidence="75"), await _suggestion(db_session, run, by, ly, confidence="80")
    await db_session.commit()
    sx_id, sy_id = str(sx.id), str(sy.id)
    default_before = [p["id"] for p in await _proposals(client, h)]
    await annotate(db_session, run, cfg, user)
    default_after = await _proposals(client, h)
    assert [p["id"] for p in default_after] == default_before                                              # scoring did not reorder the default queue
    assert all("recommendation_score" not in p for p in default_after)                                     # and added nothing to its shape
    ranked = await _proposals(client, h, order_by="recommendation")
    assert {p["id"] for p in ranked} == set(default_before)                                                # same membership: ordering only
    by_id = {p["id"]: p for p in ranked}
    assert (by_id[sx_id]["recommendation_score"], by_id[sy_id]["recommendation_score"]) == ("82.50", "80.00")
    assert (Decimal(by_id[sx_id]["confidence"]), Decimal(by_id[sy_id]["confidence"])) == (Decimal(75), Decimal(80))   # the deterministic score is reported as stored
    order = [p["id"] for p in ranked]
    assert order.index(sx_id) < order.index(sy_id)                                                         # X (82.50) now outranks Y (80.00)
    scores_in_order = [Decimal(p.get("recommendation_score") or p["confidence"]) for p in ranked]
    assert scores_in_order == sorted(scores_in_order, reverse=True)
    resp = await client.get(MP, params={"order_by": "nonsense"}, headers=h)
    assert resp.status_code == 422                                                                         # only the documented option is accepted
