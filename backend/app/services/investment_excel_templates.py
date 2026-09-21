"""
Excel Data Hub templates for Investments & Fixed Deposit Management
(SECTION 29). Same TemplateSpec pattern as
app/services/facility_excel_templates.py - the existing generic
upload/validate/preview/confirm engine (app/services/excel_service.py)
handles these with zero special-casing, including row-level entity
authorization (resolve_entity_id).
"""
import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.banking import Bank
from app.models.entity import LegalEntity
from app.models.investment import (
    DayCountConvention,
    InterestPaymentMethod,
    Investment,
    InvestmentRateType,
    InvestmentStatus,
    InvestmentTransaction,
    InvestmentTransactionStatus,
    InvestmentTransactionType,
    InvestmentType,
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


async def _investment_by_reference(db: AsyncSession, value) -> Investment | None:
    if not value:
        return None
    result = await db.execute(select(Investment).where(Investment.investment_reference == value))
    return result.scalars().first()


async def _resolve_entity_from_row(row: dict, db: AsyncSession):
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    return entity.id if entity else None


async def _resolve_entity_via_investment(row: dict, db: AsyncSession):
    investment = await _investment_by_reference(db, row.get("Investment Reference"))
    return investment.legal_entity_id if investment else None


# ---------------------------------------------------------------------------
# A. INVESTMENT MASTER
# ---------------------------------------------------------------------------
async def _validate_investment_master_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    if await _entity_by_name_or_code(db, row.get("Entity")) is None:
        issues.append(RowIssue("Entity", row.get("Entity"), "ERROR", "ENTITY_NOT_FOUND", "Entity does not exist."))
    if await _bank_by_name(db, row.get("Institution")) is None:
        issues.append(RowIssue("Institution", row.get("Institution"), "ERROR", "INSTITUTION_NOT_FOUND",
                                "Institution (bank) does not exist. Create it first."))
    if await db.get(InvestmentType, row.get("Investment Type")) is None:
        issues.append(RowIssue("Investment Type", row.get("Investment Type"), "ERROR",
                                "INVALID_INVESTMENT_TYPE", "Unknown investment type code."))
    if _parse_decimal(row.get("Principal Amount")) is None:
        issues.append(RowIssue("Principal Amount", row.get("Principal Amount"), "ERROR",
                                "INVALID_AMOUNT", "Principal Amount is required and must be numeric."))
    if _parse_date(row.get("Start Date")) is None:
        issues.append(RowIssue("Start Date", row.get("Start Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing start date."))
    if _parse_date(row.get("Maturity Date")) is None:
        issues.append(RowIssue("Maturity Date", row.get("Maturity Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing maturity date."))
    if _parse_decimal(row.get("Interest Rate")) is None:
        issues.append(RowIssue("Interest Rate", row.get("Interest Rate"), "ERROR",
                                "INVALID_RATE", "Interest Rate is required and must be numeric."))
    if not row.get("Investment Reference"):
        issues.append(RowIssue("Investment Reference", None, "ERROR", "REQUIRED_FIELD",
                                "Investment Reference is required."))
    return issues


def _investment_master_dup_key(row: dict) -> tuple:
    return ("investment_master", row.get("Investment Reference"))


async def _import_investment_master_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    bank = await _bank_by_name(db, row.get("Institution"))
    principal = _parse_decimal(row.get("Principal Amount"))
    start_date = _parse_date(row.get("Start Date"))
    maturity_date = _parse_date(row.get("Maturity Date"))
    tenor_days = (maturity_date - start_date).days if start_date and maturity_date else 0
    investment = Investment(
        investment_reference=row["Investment Reference"],
        investment_type_code=row["Investment Type"], legal_entity_id=entity.id, institution_id=bank.id,
        currency_code=(row.get("Currency") or "").upper(), principal_amount=principal,
        original_principal_amount=principal, start_date=start_date, maturity_date=maturity_date,
        tenor_days=tenor_days, interest_rate=_parse_decimal(row.get("Interest Rate")),
        rate_type=InvestmentRateType.FIXED, day_count_convention=DayCountConvention.ACT_365,
        interest_payment_method=InterestPaymentMethod.AT_MATURITY, status=InvestmentStatus.DRAFT,
    )
    db.add(investment)
    await db.flush()
    return investment


# ---------------------------------------------------------------------------
# B. INVESTMENT PLACEMENTS
# ---------------------------------------------------------------------------
async def _validate_placement_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    investment = await _investment_by_reference(db, row.get("Investment Reference"))
    if investment is None:
        issues.append(RowIssue("Investment Reference", row.get("Investment Reference"), "ERROR",
                                "INVESTMENT_NOT_FOUND", "Investment does not exist."))
    if _parse_date(row.get("Placement Date")) is None:
        issues.append(RowIssue("Placement Date", row.get("Placement Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing placement date."))
    return issues


def _placement_dup_key(row: dict) -> tuple:
    return ("investment_placement", row.get("Investment Reference"), str(row.get("Placement Date")))


async def _import_placement_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    investment = await _investment_by_reference(db, row.get("Investment Reference"))
    transaction = InvestmentTransaction(
        investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
        transaction_type=InvestmentTransactionType.PLACEMENT, currency_code=investment.currency_code,
        amount=investment.principal_amount, transaction_date=_parse_date(row.get("Placement Date")),
        status=InvestmentTransactionStatus.EXECUTED, source_type="EXCEL_UPLOAD", import_batch_id=batch_id,
    )
    db.add(transaction)
    investment.placement_date = _parse_date(row.get("Placement Date"))
    investment.status = InvestmentStatus.ACTIVE
    await db.flush()
    return transaction


# ---------------------------------------------------------------------------
# C. INVESTMENT INTEREST
# ---------------------------------------------------------------------------
async def _validate_interest_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    investment = await _investment_by_reference(db, row.get("Investment Reference"))
    if investment is None:
        issues.append(RowIssue("Investment Reference", row.get("Investment Reference"), "ERROR",
                                "INVESTMENT_NOT_FOUND", "Investment does not exist."))
    if _parse_decimal(row.get("Amount")) is None:
        issues.append(RowIssue("Amount", row.get("Amount"), "ERROR", "INVALID_AMOUNT",
                                "Amount is required and must be numeric."))
    if _parse_date(row.get("Receipt Date")) is None:
        issues.append(RowIssue("Receipt Date", row.get("Receipt Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing receipt date."))
    return issues


def _interest_dup_key(row: dict) -> tuple:
    return ("investment_interest", row.get("Investment Reference"), str(row.get("Receipt Date")),
            str(row.get("Amount")))


async def _import_interest_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    investment = await _investment_by_reference(db, row.get("Investment Reference"))
    amount = _parse_decimal(row.get("Amount"))
    transaction = InvestmentTransaction(
        investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
        transaction_type=InvestmentTransactionType.INTEREST_RECEIPT, currency_code=investment.currency_code,
        amount=amount, transaction_date=_parse_date(row.get("Receipt Date")),
        status=InvestmentTransactionStatus.EXECUTED, source_type="EXCEL_UPLOAD", import_batch_id=batch_id,
    )
    db.add(transaction)
    investment.received_interest = (investment.received_interest or Decimal(0)) + amount
    await db.flush()
    return transaction


# ---------------------------------------------------------------------------
# D. INVESTMENT TERMINATIONS
# ---------------------------------------------------------------------------
async def _validate_termination_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    investment = await _investment_by_reference(db, row.get("Investment Reference"))
    if investment is None:
        issues.append(RowIssue("Investment Reference", row.get("Investment Reference"), "ERROR",
                                "INVESTMENT_NOT_FOUND", "Investment does not exist."))
    if _parse_decimal(row.get("Amount")) is None:
        issues.append(RowIssue("Amount", row.get("Amount"), "ERROR", "INVALID_AMOUNT",
                                "Amount is required and must be numeric."))
    if _parse_date(row.get("Termination Date")) is None:
        issues.append(RowIssue("Termination Date", row.get("Termination Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing termination date."))
    return issues


def _termination_dup_key(row: dict) -> tuple:
    return ("investment_termination", row.get("Investment Reference"), str(row.get("Termination Date")),
            str(row.get("Amount")))


async def _import_termination_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    investment = await _investment_by_reference(db, row.get("Investment Reference"))
    amount = _parse_decimal(row.get("Amount"))
    is_full = amount >= investment.principal_amount
    transaction = InvestmentTransaction(
        investment_id=investment.id, legal_entity_id=investment.legal_entity_id,
        transaction_type=(
            InvestmentTransactionType.FULL_TERMINATION if is_full
            else InvestmentTransactionType.PARTIAL_TERMINATION
        ),
        currency_code=investment.currency_code, amount=amount,
        transaction_date=_parse_date(row.get("Termination Date")),
        status=InvestmentTransactionStatus.EXECUTED, source_type="EXCEL_UPLOAD", import_batch_id=batch_id,
    )
    db.add(transaction)
    investment.principal_amount = max(Decimal(0), investment.principal_amount - amount)
    investment.status = (
        InvestmentStatus.TERMINATED if investment.principal_amount == 0
        else InvestmentStatus.PARTIALLY_TERMINATED
    )
    await db.flush()
    return transaction


# ---------------------------------------------------------------------------
# E. INVESTMENT ROLLOVERS
# ---------------------------------------------------------------------------
async def _validate_rollover_row(row: dict, db: AsyncSession) -> list:
    issues: list = []
    investment = await _investment_by_reference(db, row.get("Original Investment Reference"))
    if investment is None:
        issues.append(RowIssue("Original Investment Reference", row.get("Original Investment Reference"),
                                "ERROR", "INVESTMENT_NOT_FOUND", "Original investment does not exist."))
    if not row.get("New Investment Reference"):
        issues.append(RowIssue("New Investment Reference", None, "ERROR", "REQUIRED_FIELD",
                                "New Investment Reference is required."))
    if _parse_decimal(row.get("Rollover Amount")) is None:
        issues.append(RowIssue("Rollover Amount", row.get("Rollover Amount"), "ERROR",
                                "INVALID_AMOUNT", "Rollover Amount is required and must be numeric."))
    if _parse_date(row.get("New Maturity Date")) is None:
        issues.append(RowIssue("New Maturity Date", row.get("New Maturity Date"), "ERROR",
                                "INVALID_DATE", "Invalid or missing new maturity date."))
    return issues


def _rollover_dup_key(row: dict) -> tuple:
    return ("investment_rollover", row.get("Original Investment Reference"),
            row.get("New Investment Reference"))


async def _resolve_entity_via_original_investment(row: dict, db: AsyncSession):
    investment = await _investment_by_reference(db, row.get("Original Investment Reference"))
    return investment.legal_entity_id if investment else None


async def _import_rollover_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    from app.services.investment_service import rollover_investment

    original = await _investment_by_reference(db, row.get("Original Investment Reference"))
    rollover_amount = _parse_decimal(row.get("Rollover Amount"))
    new_rate = _parse_decimal(row.get("New Rate")) or original.interest_rate
    new_maturity = _parse_date(row.get("New Maturity Date"))
    new_start = _parse_date(row.get("New Start Date")) or datetime.date.today()

    new_investment = await rollover_investment(
        db, original, rollover_amount, new_rate, new_start, new_maturity,
        row["New Investment Reference"], None,
    )
    return new_investment


INVESTMENT_TEMPLATE_REGISTRY = {
    "INVESTMENT_MASTER": TemplateSpec(
        code="INVESTMENT_MASTER", name="Investment Master", version=1,
        required_columns=["Investment Reference", "Entity", "Institution", "Investment Type",
                           "Currency", "Principal Amount", "Start Date", "Maturity Date", "Interest Rate"],
        optional_columns=[],
        validate_row=_validate_investment_master_row, duplicate_key=_investment_master_dup_key,
        import_row=_import_investment_master_row, resolve_entity_id=_resolve_entity_from_row,
    ),
    "INVESTMENT_PLACEMENTS": TemplateSpec(
        code="INVESTMENT_PLACEMENTS", name="Investment Placements", version=1,
        required_columns=["Investment Reference", "Placement Date"], optional_columns=[],
        validate_row=_validate_placement_row, duplicate_key=_placement_dup_key,
        import_row=_import_placement_row, resolve_entity_id=_resolve_entity_via_investment,
    ),
    "INVESTMENT_INTEREST": TemplateSpec(
        code="INVESTMENT_INTEREST", name="Investment Interest", version=1,
        required_columns=["Investment Reference", "Amount", "Receipt Date"], optional_columns=[],
        validate_row=_validate_interest_row, duplicate_key=_interest_dup_key,
        import_row=_import_interest_row, resolve_entity_id=_resolve_entity_via_investment,
    ),
    "INVESTMENT_TERMINATIONS": TemplateSpec(
        code="INVESTMENT_TERMINATIONS", name="Investment Terminations", version=1,
        required_columns=["Investment Reference", "Amount", "Termination Date"], optional_columns=[],
        validate_row=_validate_termination_row, duplicate_key=_termination_dup_key,
        import_row=_import_termination_row, resolve_entity_id=_resolve_entity_via_investment,
    ),
    "INVESTMENT_ROLLOVERS": TemplateSpec(
        code="INVESTMENT_ROLLOVERS", name="Investment Rollovers", version=1,
        required_columns=["Original Investment Reference", "New Investment Reference",
                           "Rollover Amount", "New Maturity Date"],
        optional_columns=["New Rate", "New Start Date"],
        validate_row=_validate_rollover_row, duplicate_key=_rollover_dup_key,
        import_row=_import_rollover_row, resolve_entity_id=_resolve_entity_via_original_investment,
    ),
}
