"""Stage 5G pure logic: settings and their hard ceilings, signal extraction, scoring maths, support levels and the tier-lock."""
import datetime
import random
import uuid
from decimal import Decimal

import pytest

from app.services.reconciliation_adaptive_learning import (
    EvidenceRow,
    PatternStat,
    aggregate,
    decay_weight,
    evaluate_pattern,
    score_adaptively,
    score_band,
    tier_locked_recommendation,
)
from app.services.reconciliation_adaptive_settings import (
    BOUNDS,
    DEFAULT_SETTINGS,
    AdaptiveSettings,
    AdaptiveSettingsError,
    parse_settings,
    validate_settings_payload,
)
from app.services.reconciliation_adaptive_signals import (
    PairView,
    amount_relation,
    narration_template,
    pair_signals,
    reference_relation,
    reference_shape,
)

D = Decimal
NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.UTC)


# ======================================================================================
# settings
# ======================================================================================
def test_adaptive_matching_is_disabled_by_default_and_when_settings_are_absent():
    assert DEFAULT_SETTINGS.enabled is False
    for config in (None, {}, {"fx_rate_type": "SPOT"}, {"adaptive": None}):
        parsed = parse_settings(config)
        assert parsed.settings.enabled is False and parsed.warnings == [] and parsed.invalid is False


def test_runtime_parsing_clamps_to_the_hard_ceilings_and_never_exceeds_them():
    parsed = parse_settings({"adaptive": {"enabled": True, "max_boost": 1000, "max_penalty": "99", "min_evidence": 1, "half_life_days": 1,
                                          "min_signal_families": 9, "max_evidence_age_days": 99999}})
    s = parsed.settings
    assert s.enabled is True and parsed.invalid is False
    assert (s.max_boost, s.max_penalty) == (D(15), D(15))                           # the ceiling, never above it
    assert (s.min_evidence, s.half_life_days, s.min_signal_families, s.max_evidence_age_days) == (3, 30, 4, 1095)
    assert len(parsed.warnings) == 6 and all("clamped" in w for w in parsed.warnings)
    low = parse_settings({"adaptive": {"max_boost": -5}}).settings
    assert low.max_boost == D(0)


@pytest.mark.parametrize("raw", [
    {"enabled": "yes"}, {"enabled": 1}, {"scope": "group"}, {"scope": "bank"}, {"max_boost": "abc"}, {"min_evidence": 5.5},
    {"min_evidence": True}, {"min_support_level": "EXTREME"}, "not-an-object", ["list"],
])
def test_unusable_settings_fail_safe_to_disabled(raw):
    parsed = parse_settings({"adaptive": raw})
    assert parsed.invalid is True and parsed.settings.enabled is False and parsed.warnings        # deterministic matching only


def test_unusable_settings_discard_even_valid_siblings():
    parsed = parse_settings({"adaptive": {"enabled": True, "scope": "group", "max_boost": 12}})
    assert parsed.settings == DEFAULT_SETTINGS                                                     # nothing partially applied


@pytest.mark.parametrize("raw, fragment", [
    ({"max_boost": 16}, "max_boost must be between"), ({"max_penalty": -1}, "max_penalty must be between"),
    ({"min_evidence": 2}, "min_evidence must be between"), ({"min_distinct_runs": 1}, "min_distinct_runs must be between"),
    ({"min_signal_families": 1}, "min_signal_families must be between"), ({"half_life_days": 10}, "half_life_days must be between"),
    ({"scope": "group"}, "only account-level"), ({"unknown_setting": 1}, "unknown setting"), ({"enabled": "true"}, "true or false"),
    ({"min_support_level": "NONE"}, "min_support_level"), ({"half_life_days": 400, "max_evidence_age_days": 100}, "must not be shorter"),
])
def test_strict_validation_rejects_instead_of_clamping(raw, fragment):
    with pytest.raises(AdaptiveSettingsError) as err:
        validate_settings_payload(raw)
    assert any(fragment in p for p in err.value.problems)


def test_strict_validation_accepts_in_bounds_values_and_every_bound_is_inclusive():
    s = validate_settings_payload({"enabled": True, "max_boost": 15, "max_penalty": "0", "min_evidence": 3, "half_life_days": 730,
                                   "max_evidence_age_days": 1095, "min_support_level": "HIGH"})
    assert s.enabled and s.max_boost == D(15) and s.max_penalty == D(0) and s.min_support_level == "HIGH"
    assert BOUNDS["max_boost"][1] == D(15)


# ======================================================================================
# signals
# ======================================================================================
def _pair(**kw):
    base = {"bank_date": datetime.date(2026, 6, 11), "ledger_date": datetime.date(2026, 6, 10), "bank_amount": D(1000), "ledger_amount": D(1000),
                "bank_references": ("NIP260000012", None), "ledger_references": ("INV-00012", None),
                "bank_narration": "NIP transfer from ACME Ltd 12", "ledger_counterparty": "Acme Ltd"}
    base.update(kw)
    return PairView(**base)


def test_reference_shapes_and_relations():
    assert reference_shape("INV-00123") == "AAA-99999" and reference_shape(" nip260000012 ") == "AAA999999999"
    assert reference_shape("") is None and reference_shape(None) is None
    assert reference_relation("INV-1", "inv-1") == "equal" and reference_relation("PAY-INV-1", "INV-1") == "contains"
    assert reference_relation("ABC", "XYZ") == "different"


def test_narration_template_keeps_only_informative_words():
    assert narration_template("NIP transfer from ACME Ltd 12345") == "nip transfer from acme"
    assert narration_template("TRF 2026 06 11 ab") == "trf"                                       # 'trf' qualifies; digits and 2-letter words do not
    assert narration_template("20260611 ab 12") is None                                           # nothing informative: no template
    assert narration_template(None) is None and narration_template("   ") is None


@pytest.mark.parametrize("bank, ledger, expected", [
    ("1000", "1000", "exact"), ("1000.50", "1000", "bank_higher:<=0.1%"), ("995", "1000", "bank_lower:<=0.5%"),
    ("990.00", "1000", "bank_lower:<=1%"), ("1100", "1000", "bank_higher:>1%"), ("5", "0", None),
])
def test_amount_relation_buckets(bank, ledger, expected):
    assert amount_relation(D(bank), D(ledger)) == expected


def test_pair_signals_covers_all_four_families_and_is_deterministic():
    signals = pair_signals(_pair())
    assert signals == {"date_offset": "+1d", "reference_format": "AAA999999999>AAA-99999:different",
                       "payee_pattern": "nip transfer from acme>acme ltd", "amount_relation": "exact"}
    assert pair_signals(_pair()) == signals


def test_a_family_is_omitted_when_its_inputs_are_missing_nothing_is_guessed():
    assert "reference_format" not in pair_signals(_pair(bank_references=(None, None)))
    assert "payee_pattern" not in pair_signals(_pair(ledger_counterparty=None))
    assert "payee_pattern" not in pair_signals(_pair(bank_narration="12 34"))
    assert "date_offset" not in pair_signals(_pair(bank_date=datetime.date(2026, 7, 1)))        # more than 10 days apart: not a lag
    assert "amount_relation" not in pair_signals(_pair(bank_amount=D(5), ledger_amount=D(0)))


# ======================================================================================
# evidence maths
# ======================================================================================
def stat(accepted, rejected=0, runs=2, age=0, half_life=180):
    s, run_ids = PatternStat(), [uuid.uuid4() for _ in range(runs)]
    for i in range(accepted + rejected):
        ok, w = i < accepted, decay_weight(age, half_life)
        s.decisions.append((uuid.uuid4(), ok, w))
        s.accepted += ok
        s.rejected += not ok
        s.aw += w if ok else D(0)
        s.rw += D(0) if ok else w
        s.runs.add(run_ids[i % runs])
    return s


def test_evidence_weight_halves_for_every_full_half_life():
    assert [decay_weight(a, 180) for a in (0, 179, 180, 359, 360, 540)] == [D(1), D(1), D("0.5"), D("0.5"), D("0.25"), D("0.125")]


def test_a_pattern_needs_enough_decisions_from_enough_runs():
    ok, balance, _ = evaluate_pattern(stat(6), DEFAULT_SETTINGS)
    assert ok and balance == D("0.75")                                                            # 6 / (6 + 2)
    assert evaluate_pattern(stat(4), DEFAULT_SETTINGS)[0] is False
    assert "only 4 earlier decision" in evaluate_pattern(stat(4), DEFAULT_SETTINGS)[2]
    assert "1 run" in evaluate_pattern(stat(6, runs=1), DEFAULT_SETTINGS)[2]


def test_the_direction_of_a_pattern_may_not_depend_on_one_decision():
    assert evaluate_pattern(stat(4, 1), DEFAULT_SETTINGS)[0] is True                              # 4 vs 1: stable
    ok, _balance, why = evaluate_pattern(stat(3, 2), DEFAULT_SETTINGS)                            # remove one accept -> an even split
    assert ok is False and "single decision" in why
    assert evaluate_pattern(stat(3, 3), DEFAULT_SETTINGS)[2] == "accepted and rejected evidence is evenly split"


def test_decay_reduces_the_weight_of_old_evidence():
    assert evaluate_pattern(stat(8, age=360), DEFAULT_SETTINGS)[1] == D("0.5")                    # 2.0 / (2.0 + 2)
    assert evaluate_pattern(stat(8, age=0), DEFAULT_SETTINGS)[1] == D("0.8")


SIGNALS = {"date_offset": "+1d", "reference_format": "R", "payee_pattern": "P", "amount_relation": "exact"}


def stats_for(families, accepted, rejected=0, runs=2, scope=("NGN", "CREDIT", "EXACT")):
    return {(*scope, f, SIGNALS[f]): stat(accepted, rejected, runs) for f in families}


def score(det, stats, settings=DEFAULT_SETTINGS, scope=("NGN", "CREDIT", "EXACT"), signals=SIGNALS):
    return score_adaptively(D(det), *scope, signals, stats, settings)


ALL4 = ("date_offset", "reference_format", "payee_pattern", "amount_relation")


def test_valid_learned_patterns_raise_the_recommendation_by_a_bounded_amount():
    out = score(60, stats_for(ALL4, 6))
    assert out.fallback is False and out.contribution == D("7.50") and out.recommendation == D("67.50") and out.band == "REVIEW"
    assert (out.accepted_examples, out.rejected_examples) == (len({d for p in out.patterns for d in p.decision_ids}), 0)
    assert [p.contribution for p in out.patterns] == [D("1.5"), D("2.25"), D("2.25"), D("1.5")]
    assert out.support_level == "MEDIUM"


def test_negative_evidence_reduces_support_and_rejections_are_never_positive():
    out = score(80, stats_for(ALL4, 0, 6))
    assert out.contribution == D("-7.50") and out.recommendation == D("72.50")
    assert all(p.contribution <= 0 for p in out.patterns) and out.rejected_examples > 0 and out.accepted_examples == 0
    assert score(80, stats_for(ALL4, 6, 0)).contribution > 0 > out.contribution


def test_one_anomalous_rejection_does_not_flip_or_dominate_a_pattern():
    clean, noisy = score(60, stats_for(ALL4, 6)), score(60, stats_for(ALL4, 6, 1))
    assert noisy.contribution > 0 and noisy.contribution < clean.contribution
    assert noisy.contribution == D("5.56")                                                        # 10 * 5/9


def test_a_lone_decision_cannot_create_a_pattern():
    out = score(60, stats_for(ALL4, 1))
    assert out.fallback and out.contribution == 0 and out.recommendation == D("60.00")
    assert "enough evidence" in out.fallback_reason and out.accepted_examples == 0


def test_a_contribution_needs_independent_signal_families():
    one = score(60, stats_for(("payee_pattern",), 20))
    assert one.fallback and "Only 1 independent signal family" in one.fallback_reason and one.contribution == 0
    assert score(60, stats_for(("payee_pattern", "date_offset"), 6)).fallback is False


def test_the_configured_cap_is_enforced():
    s = AdaptiveSettings(enabled=True, max_boost=D(3), max_penalty=D(2))
    assert score(60, stats_for(ALL4, 6), s).contribution == D("2.25")                              # 3 * 0.75
    assert score(60, stats_for(ALL4, 0, 6), s).contribution == D("-1.50")                         # 2 * -0.75
    assert score(60, stats_for(ALL4, 40), AdaptiveSettings(enabled=True, max_boost=D(15))).contribution <= D(15)
    assert score(60, stats_for(ALL4, 40), AdaptiveSettings(enabled=True, max_boost=D(0))).contribution == 0


def test_the_minimum_support_level_can_force_a_fallback():
    two = stats_for(("payee_pattern", "date_offset"), 6)
    assert score(60, two).support_level == "LOW" and score(60, two).fallback is False
    forced = score(60, two, AdaptiveSettings(enabled=True, min_support_level="MEDIUM"))
    assert forced.fallback and "below the configured minimum MEDIUM" in forced.fallback_reason and forced.contribution == 0
    assert score(60, stats_for(("payee_pattern", "date_offset", "amount_relation"), 6)).support_level == "MEDIUM"
    assert score(60, stats_for(ALL4, 20)).support_level == "HIGH"


@pytest.mark.parametrize("other", [("USD", "CREDIT", "EXACT"), ("NGN", "DEBIT", "EXACT"), ("NGN", "CREDIT", "TOLERANCE")])
def test_evidence_in_another_currency_direction_or_match_type_is_never_used(other):
    foreign = stats_for(ALL4, 30, scope=other)
    out = score(60, foreign)                                                                      # candidate is NGN / CREDIT / EXACT
    assert out.fallback and out.contribution == 0 and out.patterns == []


def test_the_same_inputs_give_the_same_result_whatever_the_evidence_order():
    rows = [EvidenceRow(uuid.uuid4(), uuid.UUID(int=i % 3), NOW - datetime.timedelta(days=i), i % 5 != 0, "NGN", "CREDIT", "EXACT", SIGNALS)
            for i in range(14)]
    first = score(60, aggregate(rows, NOW, DEFAULT_SETTINGS))
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    again = score(60, aggregate(shuffled, NOW, DEFAULT_SETTINGS))
    assert (first.contribution, first.recommendation, first.support_level, first.fallback) == \
           (again.contribution, again.recommendation, again.support_level, again.fallback)
    assert first.contribution == score(60, aggregate(rows, NOW, DEFAULT_SETTINGS)).contribution


# ======================================================================================
# tier-lock: the recommendation can never leave the deterministic score's own tier
# ======================================================================================
@pytest.mark.parametrize("det, adaptive, expected, band", [
    ("69", "10", "69.99", "REVIEW"),        # a strong boost cannot lift a review-tier score into the auto tier
    ("69", "1", "69.99", "REVIEW"), ("69", "0.5", "69.50", "REVIEW"), ("69", "-10", "59.00", "REVIEW"), ("69", "0", "69.00", "REVIEW"),
    ("70", "-10", "70.00", "AUTO"),         # a strong penalty cannot push an auto-tier score below the threshold
    ("70", "-0.01", "70.00", "AUTO"), ("70", "0", "70.00", "AUTO"), ("70", "10", "80.00", "AUTO"),
    ("100", "10", "100.00", "AUTO"), ("40", "-10", "30.00", "REVIEW"), ("0", "-10", "0.00", "REVIEW"),
])
def test_tier_lock_at_the_boundary_scores(det, adaptive, expected, band):
    assert tier_locked_recommendation(D(det), D(adaptive)) == (D(expected), band)


def test_tier_lock_holds_for_every_score_and_every_contribution():
    for det in range(101):
        for adaptive in ("-15", "-10", "-1", "-0.01", "0", "0.01", "1", "10", "15"):
            rec, band = tier_locked_recommendation(D(det), D(adaptive))
            assert band == score_band(D(det)) and (rec >= 70) == (det >= 70), (det, adaptive, rec)
            assert 0 <= rec <= 100


def test_score_bands_follow_the_stage_5c_auto_match_threshold():
    assert score_band(D(69)) == "REVIEW" and score_band(D(70)) == "AUTO"
