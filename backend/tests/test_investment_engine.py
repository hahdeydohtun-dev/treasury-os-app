import datetime
from decimal import Decimal

from app.models.investment import DayCountConvention, PenaltyType
from app.services.investment_engine import (
    calculate_early_termination,
    calculate_simple_interest,
    compare_rollover_options,
)


def test_simple_interest_day_count_conventions_differ():
    principal = Decimal(1000000)
    rate = Decimal(18)
    start = datetime.date(2026, 1, 1)
    end = datetime.date(2026, 4, 1)

    act_365 = calculate_simple_interest(principal, rate, start, end, DayCountConvention.ACT_365)
    act_360 = calculate_simple_interest(principal, rate, start, end, DayCountConvention.ACT_360)
    thirty_360 = calculate_simple_interest(principal, rate, start, end, DayCountConvention.THIRTY_360)

    assert act_360 > act_365
    assert thirty_360 > Decimal(0)


def test_simple_interest_is_pure_and_reproducible():
    args = (Decimal(500000000), Decimal("15.5"), datetime.date(2026, 1, 1),
            datetime.date(2026, 6, 1), DayCountConvention.ACT_365)
    first = calculate_simple_interest(*args)
    second = calculate_simple_interest(*args)
    assert first == second


def test_early_termination_no_penalty():
    result = calculate_early_termination(
        principal_to_terminate=Decimal(100000000), annual_rate_pct=Decimal(18),
        start_date=datetime.date(2026, 1, 1), termination_date=datetime.date(2026, 3, 1),
        convention=DayCountConvention.ACT_365, penalty_type=PenaltyType.NONE,
        penalty_rate=None, penalty_amount=None,
    )
    assert result.penalty == Decimal(0)
    assert result.interest_earned > 0
    assert result.net_proceeds == result.principal_returned + result.interest_earned


def test_early_termination_forfeit_interest_penalty():
    result = calculate_early_termination(
        principal_to_terminate=Decimal(100000000), annual_rate_pct=Decimal(18),
        start_date=datetime.date(2026, 1, 1), termination_date=datetime.date(2026, 3, 1),
        convention=DayCountConvention.ACT_365, penalty_type=PenaltyType.FORFEIT_INTEREST,
        penalty_rate=None, penalty_amount=None,
    )
    assert result.interest_earned == Decimal(0)
    assert result.penalty > 0
    assert result.net_proceeds == result.principal_returned


def test_early_termination_flat_amount_penalty_scales_with_proportion():
    full = calculate_early_termination(
        principal_to_terminate=Decimal(100000000), annual_rate_pct=Decimal(18),
        start_date=datetime.date(2026, 1, 1), termination_date=datetime.date(2026, 3, 1),
        convention=DayCountConvention.ACT_365, penalty_type=PenaltyType.FLAT_AMOUNT,
        penalty_rate=None, penalty_amount=Decimal(1000000), proportion_of_total=Decimal(1),
    )
    half = calculate_early_termination(
        principal_to_terminate=Decimal(50000000), annual_rate_pct=Decimal(18),
        start_date=datetime.date(2026, 1, 1), termination_date=datetime.date(2026, 3, 1),
        convention=DayCountConvention.ACT_365, penalty_type=PenaltyType.FLAT_AMOUNT,
        penalty_rate=None, penalty_amount=Decimal(1000000), proportion_of_total=Decimal("0.5"),
    )
    assert full.penalty == Decimal("1000000.00")
    assert half.penalty == Decimal("500000.00")


def test_early_termination_rate_reduction_penalty():
    result = calculate_early_termination(
        principal_to_terminate=Decimal(100000000), annual_rate_pct=Decimal(18),
        start_date=datetime.date(2026, 1, 1), termination_date=datetime.date(2026, 3, 1),
        convention=DayCountConvention.ACT_365, penalty_type=PenaltyType.RATE_REDUCTION,
        penalty_rate=Decimal(2), penalty_amount=None,
    )
    full_rate_interest = calculate_simple_interest(
        Decimal(100000000), Decimal(18), datetime.date(2026, 1, 1),
        datetime.date(2026, 3, 1), DayCountConvention.ACT_365,
    )
    assert result.interest_earned < full_rate_interest
    assert result.penalty > 0


def test_compare_rollover_options_never_ranks():
    comparison = compare_rollover_options(
        current_principal=Decimal(500000000), current_rate=Decimal(14),
        current_start=datetime.date(2026, 1, 1), current_maturity=datetime.date(2026, 9, 30),
        current_convention=DayCountConvention.ACT_365,
        new_principal=Decimal(400000000), new_rate=Decimal(15),
        new_start=datetime.date(2026, 10, 1), new_maturity=datetime.date(2026, 12, 30),
        new_convention=DayCountConvention.ACT_365, penalty=Decimal(0),
    )
    assert comparison.withdrawn_principal == Decimal(100000000)
    assert comparison.additional_principal == Decimal(0)
    assert comparison.current_rate == Decimal(14)
    assert comparison.proposed_rate == Decimal(15)
    assert not hasattr(comparison, "recommended")
    assert not hasattr(comparison, "best")
    assert not hasattr(comparison, "score")
