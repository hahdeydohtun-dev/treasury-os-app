"""
Investment calculation engine (Stage 4).

All financial calculations use Decimal - never float (SECTION 40/6).
Interest math reuses the same day-count-fraction approach already
established for facilities (app/services/facility_engine.py), since it
is exactly SIMPLE_INTEREST regardless of which module consumes it - the
Stage 4 model does not import from app.models.facility (no coupling
between the two investment/facility domains), but the formula itself is
the same well-established math and is deliberately re-implemented here
rather than imported, so Stage 4 has no hard dependency on Stage 3's
enum lifecycle.
"""
import datetime
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.models.investment import DayCountConvention

TWO_DP = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_DP, rounding=ROUND_HALF_UP)


def _day_count_denominator(convention: DayCountConvention) -> int:
    return {
        DayCountConvention.ACT_365: 365,
        DayCountConvention.ACT_360: 360,
        DayCountConvention.THIRTY_360: 360,
    }[convention]


def _days_between(start: datetime.date, end: datetime.date, convention: DayCountConvention) -> int:
    if convention == DayCountConvention.THIRTY_360:
        d1 = min(start.day, 30)
        d2 = min(end.day, 30) if d1 == 30 else end.day
        return (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)
    return (end - start).days


def calculate_simple_interest(
    principal: Decimal, annual_rate_pct: Decimal, start_date: datetime.date,
    end_date: datetime.date, convention: DayCountConvention,
) -> Decimal:
    """
    SECTION 6: principal x annual_rate x day-count-fraction. A pure
    function of its explicit inputs only - never reads a mutable
    Investment row - so a historical calculation is reproducible exactly
    (SECTION 6/40) regardless of any later amendment to the investment.
    """
    days = _days_between(start_date, end_date, convention)
    denominator = _day_count_denominator(convention)
    interest = principal * (annual_rate_pct / Decimal(100)) * Decimal(days) / Decimal(denominator)
    return _round(interest)


@dataclass
class TerminationResult:
    principal_returned: Decimal
    interest_earned: Decimal
    interest_forfeited: Decimal
    penalty: Decimal
    net_proceeds: Decimal


def calculate_early_termination(
    principal_to_terminate: Decimal, annual_rate_pct: Decimal, start_date: datetime.date,
    termination_date: datetime.date, convention: DayCountConvention,
    penalty_type, penalty_rate: Decimal | None, penalty_amount: Decimal | None,
    proportion_of_total: Decimal = Decimal(1),
) -> TerminationResult:
    """
    SECTION 14/15: computes principal returned, interest actually earned
    up to the termination date, interest forfeited (relative to what
    would have accrued to maturity is NOT assumed here - only interest
    earned to the termination date is ever calculated, since inventing a
    to-maturity figure the investment never reached would be exactly the
    kind of invented financial data SECTION 40/47-style principles in
    this codebase forbid), penalty, and net proceeds.

    `penalty_type` is an `app.models.investment.PenaltyType` member.
    `proportion_of_total` scales a FLAT_AMOUNT penalty for a partial
    termination (e.g. terminating 30% of principal applies 30% of a
    flat penalty) - 1 for a full termination.
    """
    from app.models.investment import PenaltyType

    interest_earned = calculate_simple_interest(
        principal_to_terminate, annual_rate_pct, start_date, termination_date, convention,
    )

    penalty = Decimal(0)
    if penalty_type == PenaltyType.FLAT_AMOUNT and penalty_amount:
        penalty = _round(penalty_amount * proportion_of_total)
    elif penalty_type == PenaltyType.RATE_REDUCTION and penalty_rate:
        reduced_rate = max(Decimal(0), annual_rate_pct - penalty_rate)
        reduced_interest = calculate_simple_interest(
            principal_to_terminate, reduced_rate, start_date, termination_date, convention,
        )
        penalty = _round(interest_earned - reduced_interest)
        interest_earned = reduced_interest
    elif penalty_type == PenaltyType.FORFEIT_INTEREST:
        penalty = interest_earned
        interest_earned = Decimal(0)

    interest_forfeited = penalty if penalty_type == PenaltyType.FORFEIT_INTEREST else Decimal(0)
    net_proceeds = _round(principal_to_terminate + interest_earned - (
        penalty if penalty_type != PenaltyType.FORFEIT_INTEREST else Decimal(0)
    ))

    return TerminationResult(
        principal_returned=principal_to_terminate, interest_earned=interest_earned,
        interest_forfeited=interest_forfeited, penalty=penalty, net_proceeds=net_proceeds,
    )


@dataclass
class RolloverComparison:
    current_rate: Decimal
    proposed_rate: Decimal
    current_maturity: datetime.date
    proposed_maturity: datetime.date
    principal: Decimal
    additional_principal: Decimal
    withdrawn_principal: Decimal
    expected_interest_current_term: Decimal
    expected_interest_new_term: Decimal
    penalty: Decimal
    net_expected_proceeds: Decimal


def compare_rollover_options(
    current_principal: Decimal, current_rate: Decimal, current_start: datetime.date,
    current_maturity: datetime.date, current_convention: DayCountConvention,
    new_principal: Decimal, new_rate: Decimal, new_start: datetime.date,
    new_maturity: datetime.date, new_convention: DayCountConvention,
    penalty: Decimal = Decimal(0),
) -> RolloverComparison:
    """
    SECTION 16/17/25: transparent comparison factors only - never an
    opaque "best option" score or ranking.
    """
    additional = max(Decimal(0), new_principal - current_principal)
    withdrawn = max(Decimal(0), current_principal - new_principal)

    expected_current = calculate_simple_interest(
        current_principal, current_rate, current_start, current_maturity, current_convention,
    )
    expected_new = calculate_simple_interest(
        new_principal, new_rate, new_start, new_maturity, new_convention,
    )
    net_proceeds = _round(new_principal + expected_new - penalty)

    return RolloverComparison(
        current_rate=current_rate, proposed_rate=new_rate, current_maturity=current_maturity,
        proposed_maturity=new_maturity, principal=new_principal, additional_principal=additional,
        withdrawn_principal=withdrawn, expected_interest_current_term=expected_current,
        expected_interest_new_term=expected_new, penalty=penalty, net_expected_proceeds=net_proceeds,
    )


_PERIODIC_FREQUENCY_MONTHS = {
    "MONTHLY": 1,
    "QUARTERLY": 3,
    "SEMI_ANNUAL": 6,
    "ANNUAL": 12,
}


def _add_months(d: datetime.date, months: int) -> datetime.date:
    import calendar

    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


@dataclass
class PeriodicInterestScheduleEntry:
    period: int
    period_start: datetime.date
    period_end: datetime.date
    interest_amount: Decimal


def generate_periodic_interest_schedule(
    principal: Decimal, annual_rate_pct: Decimal, start_date: datetime.date,
    maturity_date: datetime.date, frequency, convention: DayCountConvention,
) -> list:
    """
    SECTION 6 (Stage 4 final financial-integrity patch): deterministic
    payment dates for MONTHLY/QUARTERLY/SEMI_ANNUAL/ANNUAL - start_date
    -> each period boundary -> maturity_date, the last period always
    truncated exactly at maturity_date so nothing is double-counted or
    silently dropped as a stub period. Each period's interest uses
    calculate_simple_interest on that period's own day-count fraction -
    the same pure, reproducible math as everywhere else in this module.

    CUSTOM frequency has no stored explicit dates on the Investment model
    to build a schedule from - this deliberately returns an empty list
    rather than guessing a schedule (documented limitation, not a silent
    approximation).
    """
    frequency_value = frequency.value if hasattr(frequency, "value") else frequency
    months = _PERIODIC_FREQUENCY_MONTHS.get(frequency_value)
    if months is None or start_date >= maturity_date:
        return []

    schedule: list = []
    period_start = start_date
    period = 1
    while period_start < maturity_date:
        period_end = _add_months(period_start, months)
        period_end = min(maturity_date, period_end)
        interest = calculate_simple_interest(principal, annual_rate_pct, period_start, period_end, convention)
        schedule.append(PeriodicInterestScheduleEntry(period, period_start, period_end, interest))
        if period_end >= maturity_date:
            break
        period_start = period_end
        period += 1

    return schedule
