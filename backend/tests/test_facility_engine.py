import datetime
from decimal import Decimal

from app.models.facility import (
    CommitmentType,
    DayCountConvention,
    Facility,
    FacilityStatus,
    InterestRateType,
    RepaymentMethod,
)
from app.services.facility_engine import (
    calculate_funding_cost,
    calculate_interest,
    calculate_utilization,
    effective_interest_rate,
    evaluate_covenant,
    generate_repayment_schedule,
)


def _make_facility(**overrides) -> Facility:
    defaults = dict(
        facility_reference="TEST-001", facility_name="Test Facility",
        facility_type_code="TERM_LOAN", commitment_type=CommitmentType.COMMITTED,
        currency_code="NGN", approved_limit=Decimal(1000000000),
        committed_limit=Decimal(1000000000), current_drawn_amount=Decimal(400000000),
        covenant_restricted_amount=Decimal(0), interest_rate_type=InterestRateType.FIXED,
        fixed_rate=Decimal(20), day_count_convention=DayCountConvention.ACT_365,
        start_date=datetime.date(2026, 1, 1), maturity_date=datetime.date(2027, 1, 1),
        repayment_method=RepaymentMethod.BULLET, status=FacilityStatus.ACTIVE,
    )
    defaults.update(overrides)
    return Facility(**defaults)


def test_utilization_undrawn_and_available_differ_when_covenant_restricted():
    facility = _make_facility(covenant_restricted_amount=Decimal(200000000))
    result = calculate_utilization(facility)
    assert result.undrawn_amount == Decimal(600000000)
    assert result.available_amount == Decimal(400000000)
    assert result.utilization_pct == Decimal("40.00")


def test_uncommitted_facility_available_is_zero():
    facility = _make_facility(commitment_type=CommitmentType.UNCOMMITTED, current_drawn_amount=Decimal(0))
    result = calculate_utilization(facility)
    assert result.undrawn_amount == Decimal(1000000000)
    assert result.available_amount == Decimal(0)


def test_effective_rate_benchmark_plus_spread():
    facility = _make_facility(
        interest_rate_type=InterestRateType.BENCHMARK_PLUS_SPREAD,
        benchmark_rate=Decimal("17.5"), spread=Decimal("4.0"), fixed_rate=None,
    )
    assert effective_interest_rate(facility) == Decimal("21.5")


def test_effective_rate_missing_benchmark_returns_none():
    facility = _make_facility(
        interest_rate_type=InterestRateType.BENCHMARK_PLUS_SPREAD, benchmark_rate=None,
        spread=Decimal("4.0"), fixed_rate=None,
    )
    assert effective_interest_rate(facility) is None


def test_interest_day_count_conventions_differ():
    principal = Decimal(1000000)
    rate = Decimal(20)
    start = datetime.date(2026, 1, 1)
    end = datetime.date(2026, 4, 1)

    act_365 = calculate_interest(principal, rate, start, end, DayCountConvention.ACT_365)
    act_360 = calculate_interest(principal, rate, start, end, DayCountConvention.ACT_360)
    assert act_360 > act_365


def test_equal_principal_schedule_reduces_evenly():
    facility = _make_facility(repayment_method=RepaymentMethod.EQUAL_PRINCIPAL)
    schedule = generate_repayment_schedule(
        facility, Decimal(1200000), num_periods=4, period_days=30,
        start_date=datetime.date(2026, 1, 1),
    )
    assert len(schedule) == 4
    assert schedule[0].principal_repayment == Decimal("300000.00")
    assert schedule[-1].closing_principal == Decimal("0.00")


def test_equal_installment_schedule_has_constant_total_except_last():
    facility = _make_facility(repayment_method=RepaymentMethod.EQUAL_INSTALLMENT)
    schedule = generate_repayment_schedule(
        facility, Decimal(1000000), num_periods=6, period_days=30,
        start_date=datetime.date(2026, 1, 1),
    )
    totals = [s.total_installment for s in schedule[:-1]]
    assert len(set(totals)) == 1
    assert schedule[-1].closing_principal == Decimal("0.00")


def test_interest_only_schedule_repays_principal_only_at_end():
    facility = _make_facility(repayment_method=RepaymentMethod.INTEREST_ONLY)
    schedule = generate_repayment_schedule(
        facility, Decimal(500000), num_periods=3, period_days=30,
        start_date=datetime.date(2026, 1, 1),
    )
    assert schedule[0].principal_repayment == Decimal("0.00")
    assert schedule[1].principal_repayment == Decimal("0.00")
    assert schedule[2].principal_repayment == Decimal("500000.00")


def test_funding_cost_includes_more_than_interest():
    facility = _make_facility(commitment_fee_rate=Decimal("1.0"), committed_limit=Decimal(1000000000))
    result = calculate_funding_cost(facility, Decimal(400000000), period_days=30)
    assert result.interest > 0
    assert result.commitment_fee > 0
    assert result.total_cost == result.interest + result.commitment_fee


def test_covenant_evaluation_operators():
    assert evaluate_covenant("GTE", Decimal(100), Decimal(100)) is True
    assert evaluate_covenant("GTE", Decimal(99), Decimal(100)) is False
    assert evaluate_covenant("LTE", Decimal(50), Decimal(100)) is True
    assert evaluate_covenant("GT", Decimal(101), Decimal(100)) is True
    assert evaluate_covenant("LT", Decimal(99), Decimal(100)) is True
