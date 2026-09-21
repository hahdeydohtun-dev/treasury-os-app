"""
Excel template registry.

Each template defines:
  - required/optional columns (for structural validation + the /templates API)
  - a row validator that checks data types/references and returns
    (errors, warnings) for that row
  - an importer that, given a validated row + the current DB session,
    creates the corresponding domain record

Adding a new template means adding one entry here — the upload/validate/
import endpoints in app/api/v1/excel.py are entirely generic over this
registry, satisfying SECTION 12's "design the template architecture so
additional templates can be added without rewriting the entire Excel
engine."
"""
import datetime
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.balance import BankBalance
from app.models.bank_charge import BankCharge
from app.models.banking import Bank, BankAccount
from app.models.currency import Currency, FXRate, FXRateType
from app.models.entity import LegalEntity
from app.models.expected_cash_flow import ExpectedCollection, ExpectedPayment


@dataclass
class RowIssue:
    column: str | None
    value: str | None
    severity: str  # "ERROR" | "WARNING"
    error_code: str
    message: str


@dataclass
class TemplateSpec:
    code: str
    name: str
    version: int
    required_columns: list[str]
    optional_columns: list[str] = field(default_factory=list)
    # Returns list[RowIssue]; empty means the row is structurally valid.
    validate_row: Callable[[dict, AsyncSession], Awaitable[list[RowIssue]]] | None = None
    # Returns the natural/business duplicate key for a row (SECTION 15).
    duplicate_key: Callable[[dict], tuple] | None = None
    # Persists one validated, non-duplicate row. Returns the created object.
    import_row: (
        Callable[[dict, AsyncSession, uuid.UUID, uuid.UUID | None], Awaitable[Any]] | None
    ) = None
    # Resolves which LegalEntity a row belongs to, WITHOUT creating
    # anything - used to check row-level entity authorization before
    # import (Stage 2 Hardening, SECTION 11/TEST 10: an uploader must not
    # be able to smuggle another entity's data into an import merely
    # because the row references that entity by name). None for
    # templates with no entity concept (e.g. FX Rates).
    resolve_entity_id: Callable[[dict, AsyncSession], Awaitable[uuid.UUID | None]] | None = None


def _parse_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(value: Any) -> datetime.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


async def _currency_exists(db: AsyncSession, code: str | None) -> bool:
    if not code:
        return False
    result = await db.execute(select(Currency.code).where(Currency.code == code.upper()))
    return result.scalar_one_or_none() is not None


async def _entity_by_name_or_code(db: AsyncSession, value: str | None) -> LegalEntity | None:
    if not value:
        return None
    result = await db.execute(
        select(LegalEntity).where(
            (LegalEntity.code == value) | (LegalEntity.name == value)
        )
    )
    return result.scalars().first()


async def _resolve_entity_id_from_row(row: dict, db: AsyncSession) -> uuid.UUID | None:
    """Generic `resolve_entity_id` for every template whose rows carry an 'Entity' column."""
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    return entity.id if entity else None


async def _bank_by_name(db: AsyncSession, value: str | None) -> Bank | None:
    if not value:
        return None
    result = await db.execute(select(Bank).where(Bank.name == value))
    return result.scalars().first()


async def _bank_account_by_number(
    db: AsyncSession, account_number: str | None, bank_id: uuid.UUID | None = None
) -> BankAccount | None:
    if not account_number:
        return None
    stmt = select(BankAccount).where(BankAccount.account_number == account_number)
    if bank_id:
        stmt = stmt.where(BankAccount.bank_id == bank_id)
    result = await db.execute(stmt)
    return result.scalars().first()


# ---------------------------------------------------------------------------
# A. BANK ACCOUNTS
# ---------------------------------------------------------------------------
async def _validate_bank_account_row(row: dict, db: AsyncSession) -> list[RowIssue]:
    issues: list[RowIssue] = []
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    if entity is None:
        issues.append(RowIssue("Entity", row.get("Entity"), "ERROR", "ENTITY_NOT_FOUND",
                                "Entity does not exist."))
    bank = await _bank_by_name(db, row.get("Bank"))
    if bank is None:
        issues.append(RowIssue("Bank", row.get("Bank"), "ERROR", "BANK_NOT_FOUND",
                                "Bank does not exist. Create it first."))
    currency = (row.get("Currency") or "").upper()
    if not await _currency_exists(db, currency):
        issues.append(RowIssue("Currency", row.get("Currency"), "ERROR", "INVALID_CURRENCY",
                                "Invalid or unknown currency code."))
    if not row.get("Account Number"):
        issues.append(RowIssue("Account Number", None, "ERROR", "REQUIRED_FIELD",
                                "Account Number is required."))
    if not row.get("Account Name"):
        issues.append(RowIssue("Account Name", None, "ERROR", "REQUIRED_FIELD",
                                "Account Name is required."))
    if row.get("Minimum Operating Balance") not in (None, "") and _parse_decimal(
        row.get("Minimum Operating Balance")
    ) is None:
        issues.append(RowIssue("Minimum Operating Balance", row.get("Minimum Operating Balance"),
                                "ERROR", "INVALID_AMOUNT", "Not a valid number."))
    return issues


def _bank_account_dup_key(row: dict) -> tuple:
    return ("bank_account", row.get("Bank"), row.get("Account Number"))


async def _import_bank_account_row(
    row: dict, db: AsyncSession, batch_id: uuid.UUID, entity_scope: uuid.UUID | None
) -> BankAccount:
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    bank = await _bank_by_name(db, row.get("Bank"))
    account = BankAccount(
        legal_entity_id=entity.id,
        bank_id=bank.id,
        account_name=row["Account Name"],
        account_number=str(row["Account Number"]),
        currency_code=(row.get("Currency") or "").upper(),
        account_type_code=row.get("Account Type") or "CURRENT",
        opening_date=_parse_date(row.get("Opening Date")),
        minimum_operating_balance=_parse_decimal(row.get("Minimum Operating Balance")),
        overdraft_limit=_parse_decimal(row.get("Overdraft Limit")),
    )
    db.add(account)
    await db.flush()
    return account


# ---------------------------------------------------------------------------
# B. BANK BALANCES
# ---------------------------------------------------------------------------
async def _validate_bank_balance_row(row: dict, db: AsyncSession) -> list[RowIssue]:
    issues: list[RowIssue] = []
    account = await _bank_account_by_number(db, row.get("Account Number"))
    if account is None:
        issues.append(RowIssue("Account Number", row.get("Account Number"), "ERROR",
                                "ACCOUNT_NOT_FOUND", "Bank account does not exist."))
    if _parse_date(row.get("Balance Date")) is None:
        issues.append(RowIssue("Balance Date", row.get("Balance Date"), "ERROR",
                                "INVALID_DATE", "Invalid or missing balance date."))
    currency = (row.get("Currency") or "").upper()
    if not await _currency_exists(db, currency):
        issues.append(RowIssue("Currency", row.get("Currency"), "ERROR", "INVALID_CURRENCY",
                                "Invalid or unknown currency code."))
    if _parse_decimal(row.get("Closing Balance")) is None:
        issues.append(RowIssue("Closing Balance", row.get("Closing Balance"), "ERROR",
                                "INVALID_AMOUNT", "Closing balance is required and must be numeric."))
    return issues


def _bank_balance_dup_key(row: dict) -> tuple:
    return ("bank_balance", row.get("Account Number"), str(row.get("Balance Date")))


async def _import_bank_balance_row(
    row: dict, db: AsyncSession, batch_id: uuid.UUID, entity_scope: uuid.UUID | None
) -> BankBalance:
    account = await _bank_account_by_number(db, row.get("Account Number"))
    balance = BankBalance(
        bank_account_id=account.id,
        balance_date=_parse_date(row.get("Balance Date")),
        currency_code=(row.get("Currency") or "").upper(),
        opening_balance=_parse_decimal(row.get("Opening Balance")),
        closing_balance=_parse_decimal(row.get("Closing Balance")),
        available_balance=_parse_decimal(row.get("Available Balance")),
        ledger_balance=_parse_decimal(row.get("Ledger Balance")),
        source="EXCEL_UPLOAD",
        import_batch_id=batch_id,
    )
    db.add(balance)
    await db.flush()
    return balance


# ---------------------------------------------------------------------------
# C. BANK TRANSACTIONS
# ---------------------------------------------------------------------------
async def _validate_bank_transaction_row(row: dict, db: AsyncSession) -> list[RowIssue]:
    issues: list[RowIssue] = []
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    if entity is None:
        issues.append(RowIssue("Entity", row.get("Entity"), "ERROR", "ENTITY_NOT_FOUND",
                                "Entity does not exist."))
    account = await _bank_account_by_number(db, row.get("Account Number"))
    if account is None:
        issues.append(RowIssue("Account Number", row.get("Account Number"), "ERROR",
                                "ACCOUNT_NOT_FOUND", "Bank account does not exist."))
    if _parse_date(row.get("Transaction Date")) is None:
        issues.append(RowIssue("Transaction Date", row.get("Transaction Date"), "ERROR",
                                "INVALID_DATE", "Invalid or missing transaction date."))
    currency = (row.get("Currency") or "").upper()
    if not await _currency_exists(db, currency):
        issues.append(RowIssue("Currency", row.get("Currency"), "ERROR", "INVALID_CURRENCY",
                                "Invalid or unknown currency code."))
    if _parse_decimal(row.get("Amount")) is None:
        issues.append(RowIssue("Amount", row.get("Amount"), "ERROR", "INVALID_AMOUNT",
                                "Amount is required and must be numeric."))
    dc = (row.get("Debit/Credit") or "").upper()
    if dc not in ("DEBIT", "CREDIT", "D", "C"):
        issues.append(RowIssue("Debit/Credit", row.get("Debit/Credit"), "ERROR",
                                "INVALID_DEBIT_CREDIT",
                                "Must be DEBIT/CREDIT (or D/C)."))
    if not row.get("Reference") and not row.get("External Reference"):
        issues.append(RowIssue("Reference", None, "WARNING", "MISSING_REFERENCE",
                                "No reference or external reference provided; "
                                "reconciliation matching may be harder."))
    return issues


def _bank_transaction_dup_key(row: dict) -> tuple:
    return (
        "bank_transaction",
        row.get("Entity"),
        row.get("Account Number"),
        str(row.get("Value Date") or row.get("Transaction Date")),
        str(row.get("Amount")),
        row.get("Reference") or row.get("External Reference"),
    )


async def _import_bank_transaction_row(
    row: dict, db: AsyncSession, batch_id: uuid.UUID, entity_scope: uuid.UUID | None
):
    from app.models.lookup import CashDirection
    from app.models.treasury_transaction import TreasuryTransaction

    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    account = await _bank_account_by_number(db, row.get("Account Number"))
    dc = (row.get("Debit/Credit") or "").upper()
    direction = CashDirection.INFLOW if dc in ("CREDIT", "C") else CashDirection.OUTFLOW
    txn = TreasuryTransaction(
        legal_entity_id=entity.id,
        event_type_code="BANK_RECEIPT" if direction == CashDirection.INFLOW else "BANK_PAYMENT",
        direction=direction,
        event_date=_parse_date(row.get("Transaction Date")),
        value_date=_parse_date(row.get("Value Date")),
        transaction_currency_code=(row.get("Currency") or "").upper(),
        transaction_amount=abs(_parse_decimal(row.get("Amount"))),
        bank_account_id=account.id,
        bank_id=account.bank_id,
        counterparty=row.get("Counterparty"),
        reference=row.get("Reference"),
        external_reference=row.get("External Reference"),
        narration=row.get("Narration"),
        source_type="EXCEL_UPLOAD",
        import_batch_id=batch_id,
    )
    db.add(txn)
    await db.flush()
    return txn


# ---------------------------------------------------------------------------
# D. EXPECTED COLLECTIONS
# ---------------------------------------------------------------------------
async def _validate_expected_collection_row(row: dict, db: AsyncSession) -> list[RowIssue]:
    issues: list[RowIssue] = []
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    if entity is None:
        issues.append(RowIssue("Entity", row.get("Entity"), "ERROR", "ENTITY_NOT_FOUND",
                                "Entity does not exist."))
    if _parse_date(row.get("Expected Date")) is None:
        issues.append(RowIssue("Expected Date", row.get("Expected Date"), "ERROR",
                                "INVALID_DATE", "Invalid or missing expected date."))
    currency = (row.get("Currency") or "").upper()
    if not await _currency_exists(db, currency):
        issues.append(RowIssue("Currency", row.get("Currency"), "ERROR", "INVALID_CURRENCY",
                                "Invalid or unknown currency code."))
    if _parse_decimal(row.get("Amount")) is None:
        issues.append(RowIssue("Amount", row.get("Amount"), "ERROR", "INVALID_AMOUNT",
                                "Amount is required and must be numeric."))
    return issues


def _expected_collection_dup_key(row: dict) -> tuple:
    return (
        "expected_collection", row.get("Entity"), str(row.get("Expected Date")),
        str(row.get("Amount")), row.get("Reference"),
    )


async def _import_expected_collection_row(
    row: dict, db: AsyncSession, batch_id: uuid.UUID, entity_scope: uuid.UUID | None
) -> ExpectedCollection:
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    item = ExpectedCollection(
        legal_entity_id=entity.id,
        expected_date=_parse_date(row.get("Expected Date")),
        currency_code=(row.get("Currency") or "").upper(),
        amount=_parse_decimal(row.get("Amount")),
        counterparty=row.get("Customer/Counterparty"),
        reference=row.get("Reference"),
        category=row.get("Category"),
        probability=int(row["Probability"]) if row.get("Probability") not in (None, "") else None,
        notes=row.get("Notes"),
        source_type="EXCEL_UPLOAD",
        import_batch_id=batch_id,
    )
    db.add(item)
    await db.flush()
    return item


# ---------------------------------------------------------------------------
# E. EXPECTED PAYMENTS
# ---------------------------------------------------------------------------
async def _validate_expected_payment_row(row: dict, db: AsyncSession) -> list[RowIssue]:
    issues: list[RowIssue] = []
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    if entity is None:
        issues.append(RowIssue("Entity", row.get("Entity"), "ERROR", "ENTITY_NOT_FOUND",
                                "Entity does not exist."))
    if _parse_date(row.get("Expected Date")) is None:
        issues.append(RowIssue("Expected Date", row.get("Expected Date"), "ERROR",
                                "INVALID_DATE", "Invalid or missing expected date."))
    currency = (row.get("Currency") or "").upper()
    if not await _currency_exists(db, currency):
        issues.append(RowIssue("Currency", row.get("Currency"), "ERROR", "INVALID_CURRENCY",
                                "Invalid or unknown currency code."))
    if _parse_decimal(row.get("Amount")) is None:
        issues.append(RowIssue("Amount", row.get("Amount"), "ERROR", "INVALID_AMOUNT",
                                "Amount is required and must be numeric."))
    return issues


def _expected_payment_dup_key(row: dict) -> tuple:
    return (
        "expected_payment", row.get("Entity"), str(row.get("Expected Date")),
        str(row.get("Amount")), row.get("Reference"),
    )


async def _import_expected_payment_row(
    row: dict, db: AsyncSession, batch_id: uuid.UUID, entity_scope: uuid.UUID | None
) -> ExpectedPayment:
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    item = ExpectedPayment(
        legal_entity_id=entity.id,
        expected_date=_parse_date(row.get("Expected Date")),
        currency_code=(row.get("Currency") or "").upper(),
        amount=_parse_decimal(row.get("Amount")),
        counterparty=row.get("Supplier/Counterparty"),
        reference=row.get("Reference"),
        category=row.get("Category"),
        priority=row.get("Priority"),
        probability=int(row["Probability"]) if row.get("Probability") not in (None, "") else None,
        notes=row.get("Notes"),
        source_type="EXCEL_UPLOAD",
        import_batch_id=batch_id,
    )
    db.add(item)
    await db.flush()
    return item


# ---------------------------------------------------------------------------
# F. FX RATES  (reuses the existing FXRate model/versioning from Stage 1)
# ---------------------------------------------------------------------------
async def _validate_fx_rate_row(row: dict, db: AsyncSession) -> list[RowIssue]:
    issues: list[RowIssue] = []
    if _parse_date(row.get("Rate Date")) is None:
        issues.append(RowIssue("Rate Date", row.get("Rate Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing rate date."))
    for col in ("From Currency", "To Currency"):
        code = (row.get(col) or "").upper()
        if not await _currency_exists(db, code):
            issues.append(RowIssue(col, row.get(col), "ERROR", "INVALID_CURRENCY",
                                    "Invalid or unknown currency code."))
    if _parse_decimal(row.get("Rate")) is None:
        issues.append(RowIssue("Rate", row.get("Rate"), "ERROR", "INVALID_RATE",
                                "Rate is required and must be numeric."))
    rate_type = (row.get("Rate Type") or "SPOT").upper()
    if rate_type not in FXRateType.__members__:
        issues.append(RowIssue("Rate Type", row.get("Rate Type"), "ERROR", "INVALID_RATE_TYPE",
                                "Unrecognized rate type."))
    return issues


def _fx_rate_dup_key(row: dict) -> tuple:
    return (
        "fx_rate", row.get("From Currency"), row.get("To Currency"),
        row.get("Rate Type"), str(row.get("Rate Date")),
    )


async def _import_fx_rate_row(
    row: dict, db: AsyncSession, batch_id: uuid.UUID, entity_scope: uuid.UUID | None
) -> FXRate:
    rate_type = FXRateType[(row.get("Rate Type") or "SPOT").upper()]
    rate_date = _parse_date(row.get("Rate Date"))
    from_ccy = (row.get("From Currency") or "").upper()
    to_ccy = (row.get("To Currency") or "").upper()

    existing_stmt = select(FXRate).where(
        FXRate.from_currency_code == from_ccy,
        FXRate.to_currency_code == to_ccy,
        FXRate.rate_type == rate_type,
        FXRate.rate_date == rate_date,
        FXRate.is_current.is_(True),
    )
    existing = (await db.execute(existing_stmt)).scalars().first()
    new_version = (existing.version + 1) if existing else 1

    new_rate = FXRate(
        from_currency_code=from_ccy,
        to_currency_code=to_ccy,
        rate_type=rate_type,
        rate_date=rate_date,
        rate=_parse_decimal(row.get("Rate")),
        rate_source=row.get("Source") or "EXCEL_UPLOAD",
        version=new_version,
        is_current=True,
    )
    db.add(new_rate)
    await db.flush()
    if existing:
        existing.is_current = False
        existing.superseded_by_id = new_rate.id
    return new_rate


# ---------------------------------------------------------------------------
# G. BANK CHARGES
# ---------------------------------------------------------------------------
async def _validate_bank_charge_row(row: dict, db: AsyncSession) -> list[RowIssue]:
    issues: list[RowIssue] = []
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    if entity is None:
        issues.append(RowIssue("Entity", row.get("Entity"), "ERROR", "ENTITY_NOT_FOUND",
                                "Entity does not exist."))
    account = await _bank_account_by_number(db, row.get("Account Number"))
    if account is None:
        issues.append(RowIssue("Account Number", row.get("Account Number"), "ERROR",
                                "ACCOUNT_NOT_FOUND", "Bank account does not exist."))
    if _parse_date(row.get("Charge Date")) is None:
        issues.append(RowIssue("Charge Date", row.get("Charge Date"), "ERROR", "INVALID_DATE",
                                "Invalid or missing charge date."))
    currency = (row.get("Currency") or "").upper()
    if not await _currency_exists(db, currency):
        issues.append(RowIssue("Currency", row.get("Currency"), "ERROR", "INVALID_CURRENCY",
                                "Invalid or unknown currency code."))
    if _parse_decimal(row.get("Amount")) is None:
        issues.append(RowIssue("Amount", row.get("Amount"), "ERROR", "INVALID_AMOUNT",
                                "Amount is required and must be numeric."))
    return issues


def _bank_charge_dup_key(row: dict) -> tuple:
    return (
        "bank_charge", row.get("Account Number"), str(row.get("Charge Date")),
        str(row.get("Amount")), row.get("Reference"),
    )


async def _import_bank_charge_row(
    row: dict, db: AsyncSession, batch_id: uuid.UUID, entity_scope: uuid.UUID | None
) -> BankCharge:
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    account = await _bank_account_by_number(db, row.get("Account Number"))
    charge = BankCharge(
        legal_entity_id=entity.id,
        bank_id=account.bank_id,
        bank_account_id=account.id,
        charge_date=_parse_date(row.get("Charge Date")),
        currency_code=(row.get("Currency") or "").upper(),
        amount=_parse_decimal(row.get("Amount")),
        charge_type=row.get("Charge Type"),
        reference=row.get("Reference"),
        description=row.get("Description"),
        source_type="EXCEL_UPLOAD",
        import_batch_id=batch_id,
    )
    db.add(charge)
    await db.flush()
    return charge


TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {
    "BANK_ACCOUNTS": TemplateSpec(
        code="BANK_ACCOUNTS", name="Bank Accounts", version=1,
        required_columns=["Entity", "Bank", "Account Name", "Account Number", "Currency"],
        optional_columns=["Account Type", "Status", "Opening Date",
                           "Minimum Operating Balance", "Overdraft Limit"],
        validate_row=_validate_bank_account_row,
        duplicate_key=_bank_account_dup_key,
        import_row=_import_bank_account_row,
        resolve_entity_id=_resolve_entity_id_from_row,
    ),
    "BANK_BALANCES": TemplateSpec(
        code="BANK_BALANCES", name="Bank Balances", version=1,
        required_columns=["Entity", "Bank", "Account Number", "Balance Date",
                           "Currency", "Closing Balance"],
        optional_columns=["Opening Balance", "Available Balance", "Ledger Balance"],
        validate_row=_validate_bank_balance_row,
        duplicate_key=_bank_balance_dup_key,
        import_row=_import_bank_balance_row,
        resolve_entity_id=_resolve_entity_id_from_row,
    ),
    "BANK_TRANSACTIONS": TemplateSpec(
        code="BANK_TRANSACTIONS", name="Bank Transactions", version=1,
        required_columns=["Entity", "Bank", "Account Number", "Transaction Date",
                           "Currency", "Amount", "Debit/Credit"],
        optional_columns=["Value Date", "Reference", "External Reference", "Narration",
                           "Counterparty", "Balance After Transaction"],
        validate_row=_validate_bank_transaction_row,
        duplicate_key=_bank_transaction_dup_key,
        import_row=_import_bank_transaction_row,
        resolve_entity_id=_resolve_entity_id_from_row,
    ),
    "EXPECTED_COLLECTIONS": TemplateSpec(
        code="EXPECTED_COLLECTIONS", name="Expected Collections", version=1,
        required_columns=["Entity", "Expected Date", "Currency", "Amount"],
        optional_columns=["Customer/Counterparty", "Reference", "Category",
                           "Probability", "Status", "Notes"],
        validate_row=_validate_expected_collection_row,
        duplicate_key=_expected_collection_dup_key,
        import_row=_import_expected_collection_row,
        resolve_entity_id=_resolve_entity_id_from_row,
    ),
    "EXPECTED_PAYMENTS": TemplateSpec(
        code="EXPECTED_PAYMENTS", name="Expected Payments", version=1,
        required_columns=["Entity", "Expected Date", "Currency", "Amount"],
        optional_columns=["Supplier/Counterparty", "Reference", "Category", "Priority",
                           "Probability", "Status", "Notes"],
        validate_row=_validate_expected_payment_row,
        duplicate_key=_expected_payment_dup_key,
        import_row=_import_expected_payment_row,
        resolve_entity_id=_resolve_entity_id_from_row,
    ),
    "FX_RATES": TemplateSpec(
        code="FX_RATES", name="FX Rates", version=1,
        required_columns=["Rate Date", "From Currency", "To Currency", "Rate"],
        optional_columns=["Rate Type", "Source"],
        validate_row=_validate_fx_rate_row,
        duplicate_key=_fx_rate_dup_key,
        import_row=_import_fx_rate_row,
    ),
    "BANK_CHARGES": TemplateSpec(
        code="BANK_CHARGES", name="Bank Charges", version=1,
        required_columns=["Entity", "Bank", "Account Number", "Charge Date",
                           "Currency", "Amount"],
        optional_columns=["Charge Type", "Reference", "Description"],
        validate_row=_validate_bank_charge_row,
        duplicate_key=_bank_charge_dup_key,
        import_row=_import_bank_charge_row,
        resolve_entity_id=_resolve_entity_id_from_row,
    ),
}

# SECTION 35: Funding & Credit Facilities templates (Stage 3), merged into
# the same registry the generic upload/validate/confirm engine reads from -
# no separate ingestion pipeline for facility data.
from app.services.facility_excel_templates import FACILITY_TEMPLATE_REGISTRY

TEMPLATE_REGISTRY.update(FACILITY_TEMPLATE_REGISTRY)

# SECTION 29: Investments & Fixed Deposit Management templates (Stage 4),
# merged into the same registry the generic upload/validate/confirm
# engine reads from - no separate ingestion pipeline for investment data.
from app.services.investment_excel_templates import INVESTMENT_TEMPLATE_REGISTRY

TEMPLATE_REGISTRY.update(INVESTMENT_TEMPLATE_REGISTRY)
