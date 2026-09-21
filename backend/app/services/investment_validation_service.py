"""
Investment placement validation (SECTION 11). Returns a list of
human-readable rejection reasons - empty list means placement may
proceed. Never executes an actual bank transfer (SECTION 45) - a
validated placement is a treasury-internal record only.
"""
from decimal import Decimal

from app.models.investment import Investment, InvestmentStatus


def validate_placement(
    investment: Investment, source_account_entity_id, cash_check_amount: Decimal | None = None,
) -> list:
    reasons: list = []

    if investment.status != InvestmentStatus.APPROVED and investment.status != InvestmentStatus.PLACEMENT_PENDING:
        reasons.append(
            f"Investment must be APPROVED or PLACEMENT_PENDING to place (current status: "
            f"{investment.status.value})."
        )

    if investment.principal_amount <= 0:
        reasons.append("Principal amount must be positive.")

    if investment.maturity_date < investment.start_date:
        reasons.append("Maturity date cannot be before the start date.")

    if (
        investment.source_account_id is not None and source_account_entity_id is not None
        and source_account_entity_id != investment.legal_entity_id
    ):
        reasons.append("Source account does not belong to the investment's legal entity.")

    if investment.placement_date is None:
        reasons.append("Placement date is required.")
    elif investment.placement_date > investment.maturity_date:
        reasons.append("Placement date cannot be after the maturity date.")

    # SECTION 11: "sufficient available cash exists where configured" -
    # only checked when the caller supplies a known available-cash
    # figure; this module never invents one by querying an unrelated
    # source, since cash availability rules are entity/configuration
    # specific and out of scope for this pure validation function.
    if cash_check_amount is not None and investment.principal_amount > cash_check_amount:
        reasons.append(
            f"Principal {investment.principal_amount} exceeds available cash "
            f"{cash_check_amount} for the source account."
        )

    return reasons


def validate_termination_amount(
    outstanding_principal: Decimal, requested_amount: Decimal, allow_partial: bool,
) -> list:
    reasons: list = []
    if requested_amount <= 0:
        reasons.append("Termination amount must be positive.")
    if requested_amount > outstanding_principal:
        reasons.append(
            f"Termination amount {requested_amount} exceeds outstanding principal "
            f"{outstanding_principal}."
        )
    if not allow_partial and requested_amount < outstanding_principal:
        reasons.append("This investment does not permit partial early termination.")
    return reasons
