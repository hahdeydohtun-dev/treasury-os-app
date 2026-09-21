"""
Seeds the default ForecastCategory hierarchy (SECTION 9). Extendable via
the API/admin UI afterward - this is a starting point, not a fixed list.

Usage: python -m app.db.seed_forecast_categories
"""
import asyncio

from app.db.session import AsyncSessionLocal
from app.models.forecast import ForecastCategory
from app.models.lookup import CashDirection

CATEGORIES = [
    ("CUSTOMER_COLLECTIONS", "Customer Collections", CashDirection.INFLOW, None),
    ("ACCOUNTS_RECEIVABLE", "Accounts Receivable", CashDirection.INFLOW, None),
    ("INTERCOMPANY_RECEIPTS", "Intercompany Receipts", CashDirection.INFLOW, None),
    ("INVESTMENT_MATURITIES", "Investment Maturities", CashDirection.INFLOW, None),
    ("LOAN_DRAWDOWNS", "Loan Drawdowns", CashDirection.INFLOW, None),
    ("OTHER_INFLOW", "Other Receipts", CashDirection.INFLOW, None),

    ("SUPPLIER_PAYMENTS", "Supplier Payments", CashDirection.OUTFLOW, None),
    ("ACCOUNTS_PAYABLE", "Accounts Payable", CashDirection.OUTFLOW, None),
    ("PAYROLL", "Payroll", CashDirection.OUTFLOW, None),
    ("TAXES", "Taxes", CashDirection.OUTFLOW, None),
    ("OPERATING_EXPENSES", "Operating Expenses", CashDirection.OUTFLOW, None),
    ("CAPEX", "Capital Expenditure", CashDirection.OUTFLOW, None),
    ("DEBT_PRINCIPAL", "Debt Principal", CashDirection.OUTFLOW, None),
    ("INTEREST", "Interest", CashDirection.OUTFLOW, None),
    ("BANK_CHARGES", "Bank Charges", CashDirection.OUTFLOW, None),
    ("INTERCOMPANY_PAYMENTS", "Intercompany Payments", CashDirection.OUTFLOW, None),
    ("INVESTMENT_PLACEMENTS", "Investment Placements", CashDirection.OUTFLOW, None),
    ("OTHER_OUTFLOW", "Other Payments", CashDirection.OUTFLOW, None),

    ("INTERCOMPANY_TRANSFER", "Intercompany Transfer", CashDirection.TRANSFER, None),
]


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        for code, name, cash_type, parent in CATEGORIES:
            existing = await db.get(ForecastCategory, code)
            if existing is None:
                db.add(ForecastCategory(code=code, name=name, type=cash_type, parent_code=parent))
        await db.commit()
    print("Forecast categories seeded.")


if __name__ == "__main__":
    asyncio.run(seed())
