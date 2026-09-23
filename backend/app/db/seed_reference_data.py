"""
Seeds configuration/reference data that the application needs to function
but that is not domain/demo data: account types, cash event types, the
Currency master for common currencies, and the Excel template registry
rows (mirroring app/services/excel_templates.py).

Safe to run multiple times (upserts by primary key / code).

Usage:
    python -m app.db.seed_reference_data
"""
import asyncio

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.currency import Currency
from app.models.excel_hub import ExcelTemplate
from app.models.lookup import AccountType, CashDirection, CashEventType
from app.services.excel_templates import TEMPLATE_REGISTRY

ACCOUNT_TYPES = [
    ("CURRENT", "Current Account"),
    ("SAVINGS", "Savings Account"),
    ("COLLECTION", "Collection Account"),
    ("SETTLEMENT", "Settlement Account"),
    ("ESCROW", "Escrow Account"),
    ("FIXED_DEPOSIT", "Fixed Deposit Account"),
    ("OTHER", "Other"),
]

CASH_EVENT_TYPES = [
    ("BANK_RECEIPT", "Bank Receipt", CashDirection.INFLOW),
    ("BANK_PAYMENT", "Bank Payment", CashDirection.OUTFLOW),
    ("BANK_TRANSFER", "Bank Transfer", CashDirection.TRANSFER),
    ("CUSTOMER_COLLECTION", "Customer Collection", CashDirection.INFLOW),
    ("SUPPLIER_PAYMENT", "Supplier Payment", CashDirection.OUTFLOW),
    ("PAYROLL", "Payroll", CashDirection.OUTFLOW),
    ("TAX_PAYMENT", "Tax Payment", CashDirection.OUTFLOW),
    ("INTERCOMPANY_RECEIPT", "Intercompany Receipt", CashDirection.INFLOW),
    ("INTERCOMPANY_PAYMENT", "Intercompany Payment", CashDirection.OUTFLOW),
    ("LOAN_DRAWDOWN", "Loan Drawdown", CashDirection.INFLOW),
    ("LOAN_REPAYMENT", "Loan Repayment", CashDirection.OUTFLOW),
    ("INTEREST_PAYMENT", "Interest Payment", CashDirection.OUTFLOW),
    ("INVESTMENT_PLACEMENT", "Investment Placement", CashDirection.OUTFLOW),
    ("INVESTMENT_MATURITY", "Investment Maturity", CashDirection.INFLOW),
    ("INVESTMENT_TERMINATION", "Investment Early Termination Proceeds", CashDirection.INFLOW),
    ("INVESTMENT_INTEREST_RECEIPT", "Investment Interest Receipt", CashDirection.INFLOW),
    ("INVESTMENT_ROLLOVER", "Investment Rollover (internal, non-cash)", CashDirection.NON_CASH),
    ("BANK_FEE", "Bank Fee", CashDirection.OUTFLOW),
    ("FX_TRANSACTION", "FX Transaction", CashDirection.NON_CASH),
    ("OTHER_INFLOW", "Other Inflow", CashDirection.INFLOW),
    ("OTHER_OUTFLOW", "Other Outflow", CashDirection.OUTFLOW),
]

CURRENCIES = [
    ("NGN", "Nigerian Naira", "\u20a6"),
    ("USD", "US Dollar", "$"),
    ("GBP", "British Pound", "\u00a3"),
    ("EUR", "Euro", "\u20ac"),
]


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        for code, name in ACCOUNT_TYPES:
            existing = await db.get(AccountType, code)
            if existing is None:
                db.add(AccountType(code=code, name=name))

        for code, name, direction in CASH_EVENT_TYPES:
            existing = await db.get(CashEventType, code)
            if existing is None:
                db.add(CashEventType(code=code, name=name, default_direction=direction))

        for code, name, symbol in CURRENCIES:
            existing = await db.get(Currency, code)
            if existing is None:
                db.add(Currency(code=code, name=name, symbol=symbol, is_base_currency=True))

        await db.flush()

        for template_code, spec in TEMPLATE_REGISTRY.items():
            result = await db.execute(
                select(ExcelTemplate).where(
                    ExcelTemplate.code == template_code,
                    ExcelTemplate.version == spec.version,
                )
            )
            if result.scalars().first() is None:
                db.add(ExcelTemplate(
                    code=spec.code,
                    name=spec.name,
                    version=spec.version,
                    required_columns=spec.required_columns,
                    optional_columns=spec.optional_columns,
                ))

        await db.commit()
    print("Reference data seeded.")


if __name__ == "__main__":
    asyncio.run(seed())
