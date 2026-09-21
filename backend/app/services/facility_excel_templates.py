"""
Excel Data Hub templates for Funding & Credit Facilities data (SECTION 35).

Follows the exact same TemplateSpec pattern as app/services/excel_templates.py
(validate_row / duplicate_key / import_row / resolve_entity_id) so the
existing generic upload -> validate -> preview -> confirm engine
(app/services/excel_service.py) handles these templates with zero special
casing - including the row-level entity-authorization check added in the
Stage 2 hardening pass (resolve_entity_id is required on every
entity-bearing template here, exactly as SECTION 36/RBAC requires).
"""
import datetime
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.banking import Bank
from app.models.entity import LegalEntity
from app.models.facility import (
    CollateralType,
    CommitmentType,
    DayCountConvention,
    Facility,
    FacilityCollateral,
    FacilityCovenant,
    FacilityDrawdown,
    FacilityFee,
    FacilityRepayment,
    FacilityStatus,
    FacilityType,
    FeeType,
    InterestRateType,
    RepaymentMethod,
    RepaymentType,
)
from app.services.excel_templates import RowIssue, TemplateSpec, _parse_date, _parse_decimal


async def _entity_by_name_or_code(db: AsyncSession, value) -> LegalEntity | None:
    if not value:
        return None
    result = await db.execute(
        select(LegalEntity).where((LegalEntity.code == value) | (LegalEntity.name == value))
    )
    return result.scalars().first()


async def _bank_by_name(db: AsyncSession, value) -> Bank | None:
    if not value:
        return None
    result = await db.execute(select(Bank).where(Bank.name == value))
    return result.scalars().first()


async def _facility_by_reference(db: AsyncSession, value) -> Facility | None:
    if not value:
        return None
    result = await db.execute(select(Facility).where(Facility.facility_reference == value))
    return result.scalars().first()


async def _resolve_entity_from_row(row: dict, db: AsyncSession) -> uuid.UUID | None:
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    return entity.id if entity else None


async def _resolve_entity_via_facility(row: dict, db: AsyncSession) -> uuid.UUID | None:
    """For templates keyed by Facility Reference rather than an Entity column directly -
    the row's authorized entity is whichever entity owns the referenced facility."""
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    return facility.legal_entity_id if facility else None


# ---------------------------------------------------------------------------
# A. FACILITY MASTER
# ---------------------------------------------------------------------------
async def _validate_facility_master_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    if entity is None:
        issues.append(RowIssue("Entity", row.get("Entity"), "ERROR", "ENTITY_NOT_FOUND",
                                "Entity does not exist."))
    bank = await _bank_by_name(db, row.get("Lender"))
    if bank is None:
        issues.append(RowIssue("Lender", row.get("Lender"), "ERROR", "LENDER_NOT_FOUND",
                                "Lender (bank) does not exist. Create it first."))
    if await db.get(FacilityType, row.get("Facility Type")) is None:
        issues.append(RowIssue("Facility Type", row.get("Facility Type"), "ERROR",
                                "INVALID_FACILITY_TYPE", "Unknown facility type code."))
    if (row.get("Commitment Type") or "").upper() not in CommitmentType.__members__:
        issues.append(RowIssue("Commitment Type", row.get("Commitment Type"), "ERROR",
                                "INVALID_COMMITMENT_TYPE", "Must be COMMITTED or UNCOMMITTED."))
    if _parse_decimal(row.get("Committed Limit")) is None:
        issues.append(RowIssue("Committed Limit", row.get("Committed Limit"), "ERROR",
                                "INVALID_AMOUNT", "Committed Limit is required and must be numeric."))
    if _parse_date(row.get("Maturity Date")) is None:
        issues.append(RowIssue("Maturity Date", row.get("Maturity Date"), "ERROR",
                                "INVALID_DATE", "Invalid or missing maturity date."))
    if not row.get("Facility Reference"):
        issues.append(RowIssue("Facility Reference", None, "ERROR", "REQUIRED_FIELD",
                                "Facility Reference is required."))
    return issues


def _facility_master_dup_key(row: dict) -> tuple:
    return ("facility_master", row.get("Facility Reference"))


async def _import_facility_master_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    bank = await _bank_by_name(db, row.get("Lender"))
    rate = _parse_decimal(row.get("Fixed Rate"))
    limit = _parse_decimal(row.get("Committed Limit"))
    facility = Facility(
        facility_reference=row["Facility Reference"], facility_name=row.get("Facility Name") or row["Facility Reference"],
        facility_type_code=row["Facility Type"], commitment_type=CommitmentType((row.get("Commitment Type") or "COMMITTED").upper()),
        lender_id=bank.id, legal_entity_id=entity.id, currency_code=(row.get("Currency") or "").upper(),
        approved_limit=limit, committed_limit=limit,
        interest_rate_type=InterestRateType.FIXED if rate is not None else InterestRateType.CUSTOM,
        fixed_rate=rate, day_count_convention=DayCountConvention.ACT_365,
        start_date=_parse_date(row.get("Start Date")) or datetime.date.today(),
        maturity_date=_parse_date(row.get("Maturity Date")),
        repayment_method=RepaymentMethod.BULLET, status=FacilityStatus.DRAFT,
    )
    db.add(facility)
    await db.flush()
    return facility


# ---------------------------------------------------------------------------
# B. FACILITY DRAWDOWNS
# ---------------------------------------------------------------------------
async def _validate_drawdown_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    if facility is None:
        issues.append(RowIssue("Facility Reference", row.get("Facility Reference"), "ERROR",
                                "FACILITY_NOT_FOUND", "Facility does not exist."))
    if _parse_decimal(row.get("Drawdown Amount")) is None:
        issues.append(RowIssue("Drawdown Amount", row.get("Drawdown Amount"), "ERROR",
                                "INVALID_AMOUNT", "Drawdown Amount is required and must be numeric."))
    if _parse_date(row.get("Drawdown Date")) is None:
        issues.append(RowIssue("Drawdown Date", row.get("Drawdown Date"), "ERROR",
                                "INVALID_DATE", "Invalid or missing drawdown date."))
    return issues


def _drawdown_dup_key(row: dict) -> tuple:
    return ("facility_drawdown", row.get("Facility Reference"), row.get("Reference"),
            str(row.get("Drawdown Amount")), str(row.get("Drawdown Date")))


async def _import_drawdown_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    from app.models.facility import DrawdownStatus

    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    drawdown = FacilityDrawdown(
        facility_id=facility.id, legal_entity_id=facility.legal_entity_id,
        currency_code=facility.currency_code, drawdown_amount=_parse_decimal(row.get("Drawdown Amount")),
        drawdown_date=_parse_date(row.get("Drawdown Date")), reference=row.get("Reference"),
        purpose=row.get("Purpose"), status=DrawdownStatus.SUBMITTED,
        source_type="EXCEL_UPLOAD", import_batch_id=batch_id,
    )
    db.add(drawdown)
    await db.flush()
    return drawdown


# ---------------------------------------------------------------------------
# C. FACILITY REPAYMENTS
# ---------------------------------------------------------------------------
async def _validate_repayment_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    if facility is None:
        issues.append(RowIssue("Facility Reference", row.get("Facility Reference"), "ERROR",
                                "FACILITY_NOT_FOUND", "Facility does not exist."))
    if (row.get("Repayment Type") or "").upper() not in RepaymentType.__members__:
        issues.append(RowIssue("Repayment Type", row.get("Repayment Type"), "ERROR",
                                "INVALID_REPAYMENT_TYPE", "Must be PRINCIPAL, INTEREST, or FEE."))
    if _parse_decimal(row.get("Original Amount")) is None:
        issues.append(RowIssue("Original Amount", row.get("Original Amount"), "ERROR",
                                "INVALID_AMOUNT", "Original Amount is required and must be numeric."))
    if _parse_date(row.get("Due Date")) is None:
        issues.append(RowIssue("Due Date", row.get("Due Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing due date."))
    return issues


def _repayment_dup_key(row: dict) -> tuple:
    return ("facility_repayment", row.get("Facility Reference"), row.get("Repayment Type"),
            str(row.get("Due Date")), str(row.get("Original Amount")))


async def _import_repayment_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    repayment = FacilityRepayment(
        facility_id=facility.id, legal_entity_id=facility.legal_entity_id,
        currency_code=facility.currency_code, repayment_type=RepaymentType((row.get("Repayment Type") or "").upper()),
        original_amount=_parse_decimal(row.get("Original Amount")), due_date=_parse_date(row.get("Due Date")),
        reference=row.get("Reference"), source_type="EXCEL_UPLOAD", import_batch_id=batch_id,
    )
    db.add(repayment)
    await db.flush()
    return repayment


# ---------------------------------------------------------------------------
# D. FACILITY FEES
# ---------------------------------------------------------------------------
async def _validate_fee_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    if facility is None:
        issues.append(RowIssue("Facility Reference", row.get("Facility Reference"), "ERROR",
                                "FACILITY_NOT_FOUND", "Facility does not exist."))
    if (row.get("Fee Type") or "").upper() not in FeeType.__members__:
        issues.append(RowIssue("Fee Type", row.get("Fee Type"), "ERROR", "INVALID_FEE_TYPE",
                                "Unrecognized fee type."))
    if _parse_decimal(row.get("Amount")) is None:
        issues.append(RowIssue("Amount", row.get("Amount"), "ERROR", "INVALID_AMOUNT",
                                "Amount is required and must be numeric."))
    if _parse_date(row.get("Due Date")) is None:
        issues.append(RowIssue("Due Date", row.get("Due Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing due date."))
    return issues


def _fee_dup_key(row: dict) -> tuple:
    return ("facility_fee", row.get("Facility Reference"), row.get("Fee Type"),
            str(row.get("Due Date")), str(row.get("Amount")))


async def _import_fee_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    fee = FacilityFee(
        facility_id=facility.id, legal_entity_id=facility.legal_entity_id,
        fee_type=FeeType((row.get("Fee Type") or "").upper()), currency_code=facility.currency_code,
        amount=_parse_decimal(row.get("Amount")), due_date=_parse_date(row.get("Due Date")),
        source_type="EXCEL_UPLOAD", import_batch_id=batch_id,
    )
    db.add(fee)
    await db.flush()
    return fee


# ---------------------------------------------------------------------------
# E. FACILITY COVENANTS
# ---------------------------------------------------------------------------
async def _validate_covenant_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    if facility is None:
        issues.append(RowIssue("Facility Reference", row.get("Facility Reference"), "ERROR",
                                "FACILITY_NOT_FOUND", "Facility does not exist."))
    if not row.get("Covenant Name"):
        issues.append(RowIssue("Covenant Name", None, "ERROR", "REQUIRED_FIELD",
                                "Covenant Name is required."))
    if (row.get("Operator") or "").upper() not in ("GTE", "LTE", "GT", "LT", "EQ"):
        issues.append(RowIssue("Operator", row.get("Operator"), "ERROR", "INVALID_OPERATOR",
                                "Must be GTE, LTE, GT, LT, or EQ."))
    return issues


def _covenant_dup_key(row: dict) -> tuple:
    return ("facility_covenant", row.get("Facility Reference"), row.get("Covenant Name"))


async def _import_covenant_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    from app.models.facility import CovenantOperator, CovenantStatus, CovenantType

    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    covenant_type = row.get("Covenant Type", "CUSTOM").upper()
    if covenant_type not in CovenantType.__members__:
        covenant_type = "CUSTOM"
    covenant = FacilityCovenant(
        facility_id=facility.id, name=row["Covenant Name"], covenant_type=CovenantType(covenant_type),
        operator=CovenantOperator((row.get("Operator") or "GTE").upper()),
        threshold=_parse_decimal(row.get("Threshold")), status=CovenantStatus.DATA_REQUIRED,
    )
    db.add(covenant)
    await db.flush()
    return covenant


# ---------------------------------------------------------------------------
# F. FACILITY COLLATERAL
# ---------------------------------------------------------------------------
async def _validate_collateral_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    if facility is None:
        issues.append(RowIssue("Facility Reference", row.get("Facility Reference"), "ERROR",
                                "FACILITY_NOT_FOUND", "Facility does not exist."))
    if (row.get("Collateral Type") or "").upper() not in CollateralType.__members__:
        issues.append(RowIssue("Collateral Type", row.get("Collateral Type"), "ERROR",
                                "INVALID_COLLATERAL_TYPE", "Unrecognized collateral type."))
    if _parse_decimal(row.get("Value")) is None:
        issues.append(RowIssue("Value", row.get("Value"), "ERROR", "INVALID_AMOUNT",
                                "Value is required and must be numeric."))
    if _parse_date(row.get("Valuation Date")) is None:
        issues.append(RowIssue("Valuation Date", row.get("Valuation Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing valuation date."))
    return issues


def _collateral_dup_key(row: dict) -> tuple:
    return ("facility_collateral", row.get("Facility Reference"), row.get("Collateral Type"),
            str(row.get("Valuation Date")), str(row.get("Value")))


async def _import_collateral_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    facility = await _facility_by_reference(db, row.get("Facility Reference"))
    collateral = FacilityCollateral(
        facility_id=facility.id, collateral_type=CollateralType((row.get("Collateral Type") or "").upper()),
        value=_parse_decimal(row.get("Value")), currency_code=facility.currency_code,
        valuation_date=_parse_date(row.get("Valuation Date")),
        haircut_pct=_parse_decimal(row.get("Haircut %")) or Decimal(0),
    )
    db.add(collateral)
    await db.flush()
    return collateral


FACILITY_TEMPLATE_REGISTRY = {
    "FACILITY_MASTER": TemplateSpec(
        code="FACILITY_MASTER", name="Facility Master", version=1,
        required_columns=["Facility Reference", "Entity", "Lender", "Facility Type",
                           "Commitment Type", "Currency", "Committed Limit", "Maturity Date"],
        optional_columns=["Facility Name", "Fixed Rate", "Start Date"],
        validate_row=_validate_facility_master_row, duplicate_key=_facility_master_dup_key,
        import_row=_import_facility_master_row, resolve_entity_id=_resolve_entity_from_row,
    ),
    "FACILITY_DRAWDOWNS": TemplateSpec(
        code="FACILITY_DRAWDOWNS", name="Facility Drawdowns", version=1,
        required_columns=["Facility Reference", "Drawdown Amount", "Drawdown Date"],
        optional_columns=["Reference", "Purpose"],
        validate_row=_validate_drawdown_row, duplicate_key=_drawdown_dup_key,
        import_row=_import_drawdown_row, resolve_entity_id=_resolve_entity_via_facility,
    ),
    "FACILITY_REPAYMENTS": TemplateSpec(
        code="FACILITY_REPAYMENTS", name="Facility Repayments", version=1,
        required_columns=["Facility Reference", "Repayment Type", "Original Amount", "Due Date"],
        optional_columns=["Reference"],
        validate_row=_validate_repayment_row, duplicate_key=_repayment_dup_key,
        import_row=_import_repayment_row, resolve_entity_id=_resolve_entity_via_facility,
    ),
    "FACILITY_FEES": TemplateSpec(
        code="FACILITY_FEES", name="Facility Fees", version=1,
        required_columns=["Facility Reference", "Fee Type", "Amount", "Due Date"],
        optional_columns=[],
        validate_row=_validate_fee_row, duplicate_key=_fee_dup_key,
        import_row=_import_fee_row, resolve_entity_id=_resolve_entity_via_facility,
    ),
    "FACILITY_COVENANTS": TemplateSpec(
        code="FACILITY_COVENANTS", name="Facility Covenants", version=1,
        required_columns=["Facility Reference", "Covenant Name", "Operator"],
        optional_columns=["Covenant Type", "Threshold"],
        validate_row=_validate_covenant_row, duplicate_key=_covenant_dup_key,
        import_row=_import_covenant_row, resolve_entity_id=_resolve_entity_via_facility,
    ),
    "FACILITY_COLLATERAL": TemplateSpec(
        code="FACILITY_COLLATERAL", name="Facility Collateral", version=1,
        required_columns=["Facility Reference", "Collateral Type", "Value", "Valuation Date"],
        optional_columns=["Haircut %"],
        validate_row=_validate_collateral_row, duplicate_key=_collateral_dup_key,
        import_row=_import_collateral_row, resolve_entity_id=_resolve_entity_via_facility,
    ),
}
