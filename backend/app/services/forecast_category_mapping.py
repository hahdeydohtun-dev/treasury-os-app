"""
Maps CashEventType codes (Stage 2 TreasuryTransaction.event_type_code) and
free-text Expected Collection/Payment `category` strings onto the
configurable ForecastCategory hierarchy (SECTION 9), so actuals and
expected flows land in a consistent forecast category regardless of which
Stage 2 table they came from.

This is a best-effort default mapping, not a hard constraint: any
ForecastCategory can be created/renamed via the API, and unmapped values
fall back to OTHER_INFLOW/OTHER_OUTFLOW rather than being silently
dropped.
"""

EVENT_TYPE_TO_CATEGORY: dict[str, str] = {
    "BANK_RECEIPT": "CUSTOMER_COLLECTIONS",
    "CUSTOMER_COLLECTION": "CUSTOMER_COLLECTIONS",
    "BANK_PAYMENT": "SUPPLIER_PAYMENTS",
    "SUPPLIER_PAYMENT": "SUPPLIER_PAYMENTS",
    "BANK_TRANSFER": "INTERCOMPANY_TRANSFER",
    "INTERCOMPANY_RECEIPT": "INTERCOMPANY_RECEIPTS",
    "INTERCOMPANY_PAYMENT": "INTERCOMPANY_PAYMENTS",
    "PAYROLL": "PAYROLL",
    "TAX_PAYMENT": "TAXES",
    "LOAN_DRAWDOWN": "LOAN_DRAWDOWNS",
    "LOAN_REPAYMENT": "DEBT_PRINCIPAL",
    "INTEREST_PAYMENT": "INTEREST",
    "INVESTMENT_PLACEMENT": "INVESTMENT_PLACEMENTS",
    "INVESTMENT_MATURITY": "INVESTMENT_MATURITIES",
    "BANK_FEE": "BANK_CHARGES",
    "FX_TRANSACTION": "OTHER_OUTFLOW",
    "OTHER_INFLOW": "OTHER_INFLOW",
    "OTHER_OUTFLOW": "OTHER_OUTFLOW",
}

# Free-text `category` values on ExpectedCollection/ExpectedPayment (from
# manual entry or Excel import) mapped onto ForecastCategory codes.
FREE_TEXT_CATEGORY_TO_FORECAST_CATEGORY: dict[str, str] = {
    "TRADE RECEIVABLE": "ACCOUNTS_RECEIVABLE",
    "RECEIVABLE": "ACCOUNTS_RECEIVABLE",
    "TRADE PAYABLE": "ACCOUNTS_PAYABLE",
    "PAYABLE": "ACCOUNTS_PAYABLE",
    "PAYROLL": "PAYROLL",
    "TAX": "TAXES",
    "TAXES": "TAXES",
    "CAPEX": "CAPEX",
    "OPERATING EXPENSE": "OPERATING_EXPENSES",
    "OPEX": "OPERATING_EXPENSES",
}


def resolve_category_for_event_type(event_type_code: str, direction: str) -> str:
    return EVENT_TYPE_TO_CATEGORY.get(
        event_type_code, "OTHER_INFLOW" if direction == "INFLOW" else "OTHER_OUTFLOW"
    )


def resolve_category_for_free_text(category_text: str | None, direction: str) -> str:
    if category_text:
        mapped = FREE_TEXT_CATEGORY_TO_FORECAST_CATEGORY.get(category_text.strip().upper())
        if mapped:
            return mapped
    return (
        "CUSTOMER_COLLECTIONS" if direction == "INFLOW" else "SUPPLIER_PAYMENTS"
    )
