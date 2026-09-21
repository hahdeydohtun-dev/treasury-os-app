"""
FundingAction data integrity rules.

A FundingAction is a treasury decision/workflow record - it is NEVER
itself the financial transaction (see docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md,
"FundingAction vs. the underlying financial transaction"). When an action
IS linked to a specific transaction, that link must be internally
consistent - otherwise the action's own fields (entity, facility,
amount, currency) could silently disagree with the transaction it claims
to authorize, which is exactly the kind of financial-integrity gap this
module closes.

validate_funding_action returns a list of human-readable rejection
reasons (empty = valid) - the same pattern as
drawdown_validation_service.validate_drawdown.
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.facility import (
    Facility,
    FacilityDrawdown,
    FacilityRepayment,
    FundingAction,
    FundingActionType,
)

# SECTION 2: which action types may carry which kind of link. Types not
# listed here may carry neither - there is no documented business rule
# for a refinancing/renewal/limit-change action to authorize one specific
# drawdown or repayment.
_ALLOWED_LINK_BY_ACTION_TYPE = {
    FundingActionType.PROPOSE_DRAWDOWN: "drawdown",
    FundingActionType.PROPOSE_REPAYMENT: "repayment",
}


async def validate_funding_action(
    db: AsyncSession,
    action_type: FundingActionType,
    legal_entity_id,
    facility_id,
    linked_drawdown_id,
    linked_repayment_id,
    amount: Decimal | None,
    currency_code: str | None,
    exclude_action_id=None,
) -> list:
    reasons: list = []

    if linked_drawdown_id is not None and linked_repayment_id is not None:
        reasons.append(
            "A FundingAction cannot reference both a drawdown and a repayment at the "
            "same time - it may reference at most one underlying transaction."
        )
        return reasons

    allowed_kind = _ALLOWED_LINK_BY_ACTION_TYPE.get(action_type)
    if linked_drawdown_id is not None and allowed_kind != "drawdown":
        reasons.append(
            f"Action type {action_type.value} may not link to a drawdown - only "
            f"PROPOSE_DRAWDOWN actions may set linked_drawdown_id."
        )
    if linked_repayment_id is not None and allowed_kind != "repayment":
        reasons.append(
            f"Action type {action_type.value} may not link to a repayment - only "
            f"PROPOSE_REPAYMENT actions may set linked_repayment_id."
        )
    if reasons:
        return reasons

    if linked_drawdown_id is not None:
        drawdown = await db.get(FacilityDrawdown, linked_drawdown_id)
        if drawdown is None:
            reasons.append("linked_drawdown_id does not refer to an existing drawdown.")
            return reasons
        reasons.extend(
            await _check_linked_transaction(
                db, "drawdown", drawdown.legal_entity_id, drawdown.facility_id,
                drawdown.drawdown_amount, drawdown.currency_code,
                legal_entity_id, facility_id, amount, currency_code,
            )
        )

    if linked_repayment_id is not None:
        repayment = await db.get(FacilityRepayment, linked_repayment_id)
        if repayment is None:
            reasons.append("linked_repayment_id does not refer to an existing repayment.")
            return reasons
        reasons.extend(
            await _check_linked_transaction(
                db, "repayment", repayment.legal_entity_id, repayment.facility_id,
                repayment.original_amount, repayment.currency_code,
                legal_entity_id, facility_id, amount, currency_code,
            )
        )

    # SECTION 6: one FundingAction per underlying transaction. Documented
    # business rule chosen here: exactly one FundingAction may reference a
    # given drawdown or repayment, so a second approval workflow can never
    # be started against a transaction that already has one - combined
    # with the EXECUTED-linkage guard, this means duplicate execution
    # cannot happen via a second action either. Enforced here AND at the
    # database level (partial unique indexes) for defense in depth.
    if linked_drawdown_id is not None:
        stmt = select(FundingAction).where(FundingAction.linked_drawdown_id == linked_drawdown_id)
        if exclude_action_id is not None:
            stmt = stmt.where(FundingAction.id != exclude_action_id)
        existing = (await db.execute(stmt)).scalars().first()
        if existing is not None:
            reasons.append(
                f"Drawdown {linked_drawdown_id} is already linked to FundingAction "
                f"{existing.id} - only one FundingAction may reference a given drawdown."
            )

    if linked_repayment_id is not None:
        stmt = select(FundingAction).where(FundingAction.linked_repayment_id == linked_repayment_id)
        if exclude_action_id is not None:
            stmt = stmt.where(FundingAction.id != exclude_action_id)
        existing = (await db.execute(stmt)).scalars().first()
        if existing is not None:
            reasons.append(
                f"Repayment {linked_repayment_id} is already linked to FundingAction "
                f"{existing.id} - only one FundingAction may reference a given repayment."
            )

    return reasons


async def _check_linked_transaction(
    db: AsyncSession, kind: str, txn_entity_id, txn_facility_id, txn_amount, txn_currency,
    action_entity_id, action_facility_id, action_amount, action_currency,
) -> list:
    reasons: list = []

    if txn_entity_id != action_entity_id:
        reasons.append(
            f"FundingAction.legal_entity_id does not match the linked {kind}'s entity - "
            "a FundingAction cannot authorize another entity's transaction."
        )

    if action_facility_id is not None and txn_facility_id != action_facility_id:
        reasons.append(
            f"FundingAction.facility_id does not match the linked {kind}'s facility_id."
        )
    facility = await db.get(Facility, txn_facility_id)
    if facility is not None and facility.legal_entity_id != action_entity_id:
        reasons.append(
            f"The linked {kind}'s facility does not belong to FundingAction.legal_entity_id."
        )

    if action_currency is not None and action_currency.upper() != txn_currency.upper():
        reasons.append(
            f"FundingAction.currency_code ({action_currency}) does not match the linked "
            f"{kind}'s currency ({txn_currency})."
        )
    if action_amount is not None and action_amount != txn_amount:
        reasons.append(
            f"FundingAction.amount ({action_amount}) does not match the linked {kind}'s "
            f"amount ({txn_amount})."
        )

    return reasons
