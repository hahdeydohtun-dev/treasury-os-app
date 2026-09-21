"""
Drawdown validation (SECTION 9). Returns a list of human-readable
rejection reasons - empty list means the drawdown may proceed. Never
executes a real bank transaction (SECTION 47) - a validated, approved
drawdown is a treasury-internal record only.
"""
import datetime
from decimal import Decimal

from app.models.facility import Facility, FacilityStatus, FacilitySubLimit
from app.services.facility_engine import calculate_utilization


def validate_drawdown(
    facility: Facility, requested_amount: Decimal, requested_currency: str,
    drawdown_date: datetime.date, sub_limit: FacilitySubLimit | None = None,
) -> list:
    reasons: list = []

    if facility.status != FacilityStatus.ACTIVE:
        reasons.append(f"Facility is not ACTIVE (current status: {facility.status.value}).")

    if requested_currency.upper() != facility.currency_code:
        reasons.append(
            f"Requested currency {requested_currency} does not match the facility's "
            f"currency {facility.currency_code}."
        )

    if facility.availability_start_date and drawdown_date < facility.availability_start_date:
        reasons.append("Drawdown date is before the facility's availability start date.")
    if facility.availability_end_date and drawdown_date > facility.availability_end_date:
        reasons.append("Drawdown date is after the facility's availability end date.")
    if drawdown_date > facility.maturity_date:
        reasons.append("Drawdown date is after the facility's maturity date.")

    utilization = calculate_utilization(facility)
    if facility.commitment_type.value == "UNCOMMITTED":
        if requested_amount > utilization.undrawn_amount:
            reasons.append(
                f"Requested amount {requested_amount} exceeds undrawn capacity "
                f"{utilization.undrawn_amount} (uncommitted facility - not guaranteed)."
            )
    elif requested_amount > utilization.available_amount:
        reasons.append(
            f"Requested amount {requested_amount} exceeds available capacity "
            f"{utilization.available_amount} (undrawn {utilization.undrawn_amount} less "
            f"covenant-restricted {utilization.covenant_restricted_amount})."
        )

    if sub_limit is not None:
        # SECTION 15: a purpose-restricted sub-limit constrains a drawdown
        # independently of - and in addition to - the facility's overall
        # available capacity. A drawdown that fits under the facility's
        # total but not under the specific sub-limit it's drawn against
        # must still be rejected.
        sub_limit_remaining = sub_limit.limit_amount - sub_limit.drawn_amount
        if requested_amount > sub_limit_remaining:
            reasons.append(
                f"Requested amount {requested_amount} exceeds sub-limit '{sub_limit.name}' "
                f"remaining capacity {sub_limit_remaining} (sub-limit {sub_limit.limit_amount}, "
                f"already drawn {sub_limit.drawn_amount})."
            )

    return reasons
