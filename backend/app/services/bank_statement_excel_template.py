"""
Excel Data Hub template for Bank Statement ingestion (Stage 5A).

Follows the exact same TemplateSpec pattern as every other template
(app/services/facility_excel_templates.py,
app/services/investment_excel_templates.py) - the existing generic
upload/validate/preview/confirm engine (app/services/excel_service.py)
handles this with zero special-casing, including row-level entity
authorization (resolve_entity_id) and within-file duplicate detection
(duplicate_key).

IMPORTANT SCOPE BOUNDARY: this module produces `BankStatementTransaction`
rows only - external bank evidence. It never creates a
`TreasuryTransaction`, never touches `BankBalance`, and never affects
operational available cash or any investment/facility calculation.
Matching this evidence against TreasuryTransaction is Stage 5C's job,
not this module's.
"""
import datetime
import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_statement import (
    BankStatementEntryType,
    BankStatementTransaction,
    BankStatementTransactionStatus,
)
from app.models.banking import Bank, BankAccount
from app.models.entity import LegalEntity
from app.services.excel_templates import (
    RowIssue,
    TemplateSpec,
    _currency_exists,
    _parse_date,
    _parse_decimal,
)

# --- Debit/Credit normalization (SECTION 10) ---
_DEBIT_ALIASES = {"DEBIT", "DR", "D"}
_CREDIT_ALIASES = {"CREDIT", "CR", "C"}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _normalize_entry_type(value: Any) -> BankStatementEntryType | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in _DEBIT_ALIASES:
        return BankStatementEntryType.DEBIT
    if text in _CREDIT_ALIASES:
        return BankStatementEntryType.CREDIT
    return None


def _parse_statement_date(value: Any) -> datetime.date | None:
    """
    SECTION 10: reuses the shared `_parse_date` helper FIRST (native
    openpyxl date/datetime objects, and ISO format) - this function never
    duplicates that logic, only falls back to it. The additional
    fallbacks below exist because real bank statements frequently export
    dates as plain text in a handful of common non-ISO formats
    (DD/MM/YYYY, MM/DD/YYYY, DD-Mon-YYYY); these fallbacks are added HERE,
    specific to this template, rather than into the shared `_parse_date`
    helper, precisely so no other existing template's date parsing
    behavior can be affected by them.
    """
    parsed = _parse_date(value)
    if parsed is not None:
        return parsed
    if value in (None, ""):
        return None
    text = str(value).strip()

    # DD-Mon-YYYY (e.g. "24-Sep-2026")
    parts = text.replace("/", "-").split("-")
    if len(parts) == 3 and parts[1].strip().isalpha() and len(parts[1].strip()) == 3:
        month = _MONTHS.get(parts[1].strip().upper())
        if month:
            try:
                return datetime.date(int(parts[2]), month, int(parts[0]))
            except ValueError:
                return None

    # DD/MM/YYYY or MM/DD/YYYY (day-first is assumed unless the first
    # segment cannot be a day, i.e. > 31, or unless the first segment is
    # <= 12 and the second is > 12, which can only be MM/DD).
    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
        a, b, c = (int(p) for p in parts)
        try:
            if a > 12 and b <= 12:
                return datetime.date(c, b, a)  # unambiguous DD/MM/YYYY
            if b > 12 and a <= 12:
                return datetime.date(c, a, b)  # unambiguous MM/DD/YYYY
            # ambiguous (both <= 12): default to day-first, the more
            # common convention outside the US, matching SECTION 10's own
            # first-listed example ("24/09/2026").
            return datetime.date(c, b, a)
        except ValueError:
            return None
    return None


async def _entity_by_name_or_code(db: AsyncSession, value: Any) -> LegalEntity | None:
    if not value:
        return None
    result = await db.execute(
        select(LegalEntity).where((LegalEntity.code == value) | (LegalEntity.name == value))
    )
    return result.scalars().first()


async def _bank_by_name(db: AsyncSession, value: Any) -> Bank | None:
    if not value:
        return None
    result = await db.execute(select(Bank).where(Bank.name == value))
    return result.scalars().first()


async def _bank_account_by_number(db: AsyncSession, account_number: Any) -> BankAccount | None:
    if not account_number:
        return None
    result = await db.execute(
        select(BankAccount).where(BankAccount.account_number == str(account_number))
    )
    return result.scalars().first()


def _compute_duplicate_key(
    bank_account_key: str, transaction_date: Any, entry_type: Any, amount: Any,
    currency: Any, bank_reference: Any, external_transaction_id: Any, narration: Any,
) -> tuple[str, bool]:
    """
    SECTION 11/12: the deterministic duplicate identity. `has_strong_identity`
    is true only when a bank_reference or external_transaction_id is
    present - two rows sharing a key WITHOUT either are, at most,
    POTENTIAL_DUPLICATE (insufficient identity to declare an EXACT
    duplicate), never silently treated as certainly the same transaction.
    Deliberately does NOT use "same date + same amount" alone (SECTION 12).
    """
    has_strong_identity = bool(bank_reference) or bool(external_transaction_id)
    parts = [
        str(bank_account_key), str(transaction_date), str(entry_type), str(amount), str(currency),
        str(bank_reference or ""), str(external_transaction_id or ""),
        str(narration or "") if not has_strong_identity else "",
        # narration only participates in the key when there is no
        # reference at all - once a reference exists, narration is
        # cosmetic and must not cause two rows with the same real
        # reference to be treated as different transactions.
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest, has_strong_identity


async def _validate_bank_statement_row(row: dict, db: AsyncSession) -> list:
    issues: list = []

    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    if entity is None:
        issues.append(RowIssue("Entity", row.get("Entity"), "ERROR", "ENTITY_NOT_FOUND",
                                "Entity does not exist."))

    bank_account = await _bank_account_by_number(db, row.get("Bank Account"))
    if bank_account is None:
        issues.append(RowIssue("Bank Account", row.get("Bank Account"), "ERROR",
                                "BANK_ACCOUNT_NOT_FOUND", "Bank account does not exist."))
    elif entity is not None and bank_account.legal_entity_id != entity.id:
        issues.append(RowIssue("Bank Account", row.get("Bank Account"), "ERROR",
                                "BANK_ACCOUNT_NOT_AUTHORIZED",
                                "This bank account does not belong to the specified entity."))

    period_start = _parse_statement_date(row.get("Statement Period Start"))
    period_end = _parse_statement_date(row.get("Statement Period End"))
    if period_start is None or period_end is None:
        issues.append(RowIssue("Statement Period", None, "ERROR", "INVALID_DATE",
                                "Statement Period Start/End are required and must be valid dates."))
    elif period_start > period_end:
        issues.append(RowIssue("Statement Period", None, "ERROR", "INVALID_STATEMENT_PERIOD",
                                "Statement Period Start must not be after Statement Period End."))

    txn_date = _parse_statement_date(row.get("Transaction Date"))
    if txn_date is None:
        issues.append(RowIssue("Transaction Date", row.get("Transaction Date"), "ERROR",
                                "INVALID_DATE", "Transaction Date is required and must be a valid date."))
    elif period_start is not None and period_end is not None and not (period_start <= txn_date <= period_end):
        issues.append(RowIssue("Transaction Date", row.get("Transaction Date"), "ERROR",
                                "DATE_OUTSIDE_STATEMENT_PERIOD",
                                "Transaction Date falls outside the declared statement period."))

    amount = _parse_decimal(row.get("Amount"))
    if amount is None or amount <= 0:
        issues.append(RowIssue("Amount", row.get("Amount"), "ERROR", "INVALID_AMOUNT",
                                "Amount is required and must be a positive number."))

    currency = row.get("Currency")
    if not currency or not await _currency_exists(db, str(currency)):
        issues.append(RowIssue("Currency", currency, "ERROR", "INVALID_CURRENCY",
                                "Currency is required and must be a known currency code."))

    entry_type = _normalize_entry_type(row.get("Debit/Credit"))
    if entry_type is None:
        issues.append(RowIssue("Debit/Credit", row.get("Debit/Credit"), "ERROR",
                                "INVALID_DEBIT_CREDIT",
                                "Debit/Credit is required and must be one of DEBIT/CREDIT/DR/CR/D/C."))

    # --- Cross-batch duplicate detection (SECTION 11.B) ---
    # Within-file duplicates are already handled generically by
    # excel_service.py via TemplateSpec.duplicate_key; this check is
    # ADDITIONAL - it looks at already-committed evidence from PRIOR
    # imports, which the generic engine's own duplicate_key mechanism
    # cannot see (it only tracks keys seen within the current file).
    if bank_account is not None and txn_date is not None and amount is not None and entry_type is not None and currency:
        key, has_strong_identity = _compute_duplicate_key(
            str(bank_account.id), txn_date.isoformat(), entry_type.value, str(amount), str(currency).upper(),
            row.get("Bank Reference"), row.get("External Transaction ID"), row.get("Narration"),
        )
        existing = await db.execute(
            select(BankStatementTransaction.id).where(BankStatementTransaction.duplicate_key == key)
        )
        if existing.scalar_one_or_none() is not None:
            if has_strong_identity:
                issues.append(RowIssue(None, None, "ERROR", "DUPLICATE_TRANSACTION",
                                        "This transaction has already been imported from a prior "
                                        "statement (matched by bank reference/external transaction ID, "
                                        "account, date, amount, currency and direction)."))
            else:
                issues.append(RowIssue(None, None, "WARNING", "POTENTIAL_DUPLICATE",
                                        "A transaction with the same account, date, amount, currency "
                                        "and direction was already imported, but no bank reference or "
                                        "external transaction ID is present to confirm this is the same "
                                        "transaction. Imported for review rather than silently discarded."))

    return issues


def _bank_statement_duplicate_key(row: dict) -> tuple:
    """
    SECTION 11.A (within-file duplicates), used by the generic engine's
    own seen-keys mechanism. Built from RAW row text (not resolved
    entity/account IDs) since this function is synchronous and has no DB
    access - two rows in the same file naming the same account/reference
    text are treated identically for this purpose. Narration participates
    in the key ONLY when neither reference field is present, mirroring
    _compute_duplicate_key's identical design principle (SECTION 12): two
    transactions with the same date/amount/currency/direction but
    genuinely different narrations, and no reference at all, must NOT be
    treated as the same transaction merely because they share those four
    fields.
    """
    has_reference = bool(row.get("Bank Reference")) or bool(row.get("External Transaction ID"))
    return (
        str(row.get("Bank Account") or ""), str(row.get("Transaction Date") or ""),
        str(row.get("Debit/Credit") or "").strip().upper(), str(row.get("Amount") or ""),
        str(row.get("Currency") or "").strip().upper(),
        str(row.get("Bank Reference") or ""), str(row.get("External Transaction ID") or ""),
        str(row.get("Narration") or "") if not has_reference else "",
    )


async def _resolve_bank_statement_entity(row: dict, db: AsyncSession):
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    return entity.id if entity else None


async def _import_bank_statement_row(row: dict, db: AsyncSession, batch_id, entity_scope):
    entity = await _entity_by_name_or_code(db, row.get("Entity"))
    bank_account = await _bank_account_by_number(db, row.get("Bank Account"))
    period_start = _parse_statement_date(row.get("Statement Period Start"))
    period_end = _parse_statement_date(row.get("Statement Period End"))
    txn_date = _parse_statement_date(row.get("Transaction Date"))
    value_date = _parse_statement_date(row.get("Value Date"))
    posting_date = _parse_statement_date(row.get("Posting Date"))
    amount = _parse_decimal(row.get("Amount"))
    currency = str(row.get("Currency")).upper()
    entry_type = _normalize_entry_type(row.get("Debit/Credit"))
    balance = _parse_decimal(row.get("Balance"))

    key, has_strong_identity = _compute_duplicate_key(
        str(bank_account.id), txn_date.isoformat(), entry_type.value, str(amount), currency,
        row.get("Bank Reference"), row.get("External Transaction ID"), row.get("Narration"),
    )

    statement_txn = BankStatementTransaction(
        legal_entity_id=entity.id, bank_id=bank_account.bank_id, bank_account_id=bank_account.id,
        statement_period_start=period_start, statement_period_end=period_end,
        transaction_date=txn_date, value_date=value_date, posting_date=posting_date,
        entry_type=entry_type, amount=amount, currency_code=currency,
        balance_after_transaction=balance,
        bank_reference=row.get("Bank Reference"), external_transaction_id=row.get("External Transaction ID"),
        narration=row.get("Narration"), duplicate_key=key, has_strong_identity=has_strong_identity,
        status=BankStatementTransactionStatus.ACTIVE, import_batch_id=batch_id,
        source_row_number=row.get("__row__", 0),
    )
    db.add(statement_txn)
    await db.flush()
    return statement_txn


BANK_STATEMENT_TEMPLATE = TemplateSpec(
    code="BANK_STATEMENT", name="Bank Statement", version=1,
    required_columns=[
        "Entity", "Bank Account", "Statement Period Start", "Statement Period End",
        "Transaction Date", "Debit/Credit", "Amount", "Currency",
    ],
    optional_columns=[
        "Value Date", "Posting Date", "Balance", "Bank Reference",
        "External Transaction ID", "Narration",
    ],
    validate_row=_validate_bank_statement_row, duplicate_key=_bank_statement_duplicate_key,
    import_row=_import_bank_statement_row, resolve_entity_id=_resolve_bank_statement_entity,
)

BANK_STATEMENT_TEMPLATE_REGISTRY = {"BANK_STATEMENT": BANK_STATEMENT_TEMPLATE}
