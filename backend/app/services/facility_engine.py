"""
Facility calculation engine (Stage 3).

All financial calculations use Decimal - never float (SECTION 4/45).
See docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md for the full methodology.
"""
import datetime
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.models.facility import DayCountConvention, Facility, InterestRateType

TWO_DP = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_DP, rounding=ROUND_HALF_UP)


@dataclass
class UtilizationResult:
    committed_limit: Decimal
    drawn_amount: Decimal
    undrawn_amount: Decimal
    covenant_restricted_amount: Decimal
    available_amount: Decimal
    utilization_pct: Decimal


def calculate_utilization(facility: Facility) -> UtilizationResult:
    """
    SECTION 14: available is NOT assumed to equal undrawn. For an
    UNCOMMITTED facility, "available" is deliberately reported as 0 -
    uncommitted capacity is never treated as guaranteed liquidity
    (SECTION 3) and must be surfaced separately, never folded into this
    figure.
    """
    limit = facility.committed_limit
    drawn = facility.current_drawn_amount
    undrawn = limit - drawn
    restricted = facility.covenant_restricted_amount

    if facility.commitment_type.value == "UNCOMMITTED":
        available = Decimal(0)
    else:
        available = max(Decimal(0), undrawn - restricted)

    utilization_pct = (drawn / limit * 100) if limit > 0 else Decimal(0)

    return UtilizationResult(
        committed_limit=limit, drawn_amount=drawn, undrawn_amount=undrawn,
        covenant_restricted_amount=restricted, available_amount=_round(available),
        utilization_pct=_round(utilization_pct),
    )


def effective_interest_rate(facility: Facility) -> Decimal | None:
    """SECTION 12: benchmark + spread, fixed, or custom - never assumed."""
    if facility.interest_rate_type == InterestRateType.FIXED:
        return facility.fixed_rate
    if facility.interest_rate_type == InterestRateType.BENCHMARK_PLUS_SPREAD:
        if facility.benchmark_rate is None or facility.spread is None:
            return None
        return facility.benchmark_rate + facility.spread
    if facility.interest_rate_type == InterestRateType.VARIABLE:
        return facility.benchmark_rate
    return facility.fixed_rate


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


def calculate_interest(
    principal: Decimal, annual_rate_pct: Decimal, start_date: datetime.date,
    end_date: datetime.date, convention: DayCountConvention,
) -> Decimal:
    days = _days_between(start_date, end_date, convention)
    denominator = _day_count_denominator(convention)
    interest = principal * (annual_rate_pct / Decimal(100)) * Decimal(days) / Decimal(denominator)
    return _round(interest)


@dataclass
class ScheduleInstallment:
    period: int
    due_date: datetime.date
    opening_principal: Decimal
    principal_repayment: Decimal
    interest: Decimal
    total_installment: Decimal
    closing_principal: Decimal


def generate_repayment_schedule(
    facility: Facility, principal: Decimal, num_periods: int, period_days: int,
    start_date: datetime.date,
) -> list:
    """SECTION 11: EQUAL_PRINCIPAL, EQUAL_INSTALLMENT (amortizing), or INTEREST_ONLY."""
    rate = effective_interest_rate(facility)
    if rate is None:
        rate = Decimal(0)
    method = facility.repayment_method
    convention = facility.day_count_convention

    schedule: list = []
    opening = principal

    if method.value == "EQUAL_PRINCIPAL":
        principal_per_period = _round(principal / num_periods)
        for i in range(1, num_periods + 1):
            due = start_date + datetime.timedelta(days=period_days * i)
            period_start = start_date + datetime.timedelta(days=period_days * (i - 1))
            interest = calculate_interest(opening, rate, period_start, due, convention)
            principal_repay = principal_per_period if i < num_periods else opening
            total = _round(principal_repay + interest)
            closing = _round(opening - principal_repay)
            schedule.append(ScheduleInstallment(i, due, _round(opening), _round(principal_repay),
                                                 interest, total, closing))
            opening = closing

    elif method.value == "EQUAL_INSTALLMENT":
        period_rate = (rate / Decimal(100)) * Decimal(period_days) / Decimal(
            _day_count_denominator(convention)
        )
        if period_rate == 0:
            installment = _round(principal / num_periods)
        else:
            factor = (1 + period_rate) ** num_periods
            installment = _round(principal * period_rate * factor / (factor - 1))
        for i in range(1, num_periods + 1):
            due = start_date + datetime.timedelta(days=period_days * i)
            period_start = start_date + datetime.timedelta(days=period_days * (i - 1))
            interest = calculate_interest(opening, rate, period_start, due, convention)
            principal_repay = installment - interest
            if i == num_periods:
                principal_repay = opening
                total = _round(principal_repay + interest)
            else:
                total = installment
            closing = _round(opening - principal_repay)
            schedule.append(ScheduleInstallment(i, due, _round(opening), _round(principal_repay),
                                                 interest, total, closing))
            opening = closing

    elif method.value == "INTEREST_ONLY":
        for i in range(1, num_periods + 1):
            due = start_date + datetime.timedelta(days=period_days * i)
            period_start = start_date + datetime.timedelta(days=period_days * (i - 1))
            interest = calculate_interest(opening, rate, period_start, due, convention)
            principal_repay = opening if i == num_periods else Decimal(0)
            total = _round(principal_repay + interest)
            closing = _round(opening - principal_repay)
            schedule.append(ScheduleInstallment(i, due, _round(opening), _round(principal_repay),
                                                 interest, total, closing))
            opening = closing

    return schedule


@dataclass
class FundingCostResult:
    interest: Decimal
    commitment_fee: Decimal
    arrangement_fee: Decimal
    processing_fee: Decimal
    other_fees: Decimal
    total_cost: Decimal
    average_drawn: Decimal
    effective_cost_pct: Decimal | None


def calculate_funding_cost(
    facility: Facility, average_drawn: Decimal, period_days: int,
    other_fees: Decimal = Decimal(0), include_arrangement_fee: bool = False,
) -> FundingCostResult:
    """SECTION 13: cost is never "interest alone." """
    rate = effective_interest_rate(facility) or Decimal(0)
    today = datetime.date.today()
    interest = calculate_interest(
        average_drawn, rate, today, today + datetime.timedelta(days=period_days),
        facility.day_count_convention,
    )

    undrawn = max(Decimal(0), facility.committed_limit - average_drawn)
    commitment_fee = Decimal(0)
    if facility.commitment_fee_rate:
        commitment_fee = calculate_interest(
            undrawn, facility.commitment_fee_rate, today,
            today + datetime.timedelta(days=period_days), facility.day_count_convention,
        )

    arrangement = _round(facility.arrangement_fee or Decimal(0)) if include_arrangement_fee else Decimal(0)
    processing = Decimal(0)

    total = interest + commitment_fee + arrangement + processing + other_fees
    effective_cost_pct = (
        (total / average_drawn * Decimal(365) / Decimal(period_days) * 100)
        if average_drawn > 0 else None
    )

    return FundingCostResult(
        interest=interest, commitment_fee=commitment_fee, arrangement_fee=arrangement,
        processing_fee=processing, other_fees=other_fees, total_cost=_round(total),
        average_drawn=average_drawn,
        effective_cost_pct=_round(effective_cost_pct) if effective_cost_pct is not None else None,
    )


def evaluate_covenant(operator: str, current_value: Decimal, threshold: Decimal) -> bool:
    if operator == "GTE":
        return current_value >= threshold
    if operator == "LTE":
        return current_value <= threshold
    if operator == "GT":
        return current_value > threshold
    if operator == "LT":
        return current_value < threshold
    if operator == "EQ":
        return current_value == threshold
    return False
