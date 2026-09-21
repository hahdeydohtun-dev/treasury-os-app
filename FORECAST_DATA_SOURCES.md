# 13-Week Cash Flow Forecast - Data Sources

Which Stage 0/1 tables feed the forecast, exactly how, and what's
reserved for future modules (SECTION 2/22/56).

## Currently consumed

| Source table | Feeds | How |
|---|---|---|
| bank_balances | Opening cash (week 1) | Latest balance per account, per entity, as of the day before the forecast start date, via the existing Cash Position service |
| expected_collections | Forecast lines (INFLOW) | Every row with expected_date in the horizon and status != CANCELLED |
| expected_payments | Forecast lines (OUTFLOW) | Same date/status rule |
| treasury_transactions | Forecast lines (ACTUAL) | Every POSTED transaction with event_date in the horizon; classified INFLOW/OUTFLOW/TRANSFER per its own direction |
| recurring_cash_flows | Forecast lines (RECURRING) | Expanded into concrete occurrence dates within the horizon |
| forecast_adjustments | Forecast lines (MANUAL_ADJUSTMENT) | Every row with status APPLIED or APPROVED, for that specific forecast |
| fx_rates | Currency conversion | Latest current SPOT rate on or before the relevant week-end date, for every non-reporting-currency line and for opening cash |
| legal_entities | Scope resolution | A forecast's entities are either the one legal_entity_id it's scoped to, or every active entity in its group |
| currencies | Validation | Currency codes on every line are real Currency rows (enforced by the foreign key, same as everywhere else in the schema) |

## Source-type traceability

Every ForecastLine records source_type and source_id, so any amount in
the forecast can be traced back to the exact originating row:

```
EXPECTED_COLLECTION -> expected_collections.id
EXPECTED_PAYMENT     -> expected_payments.id
RECURRING            -> recurring_cash_flows.id
ACTUAL               -> treasury_transactions.id
MANUAL_ADJUSTMENT    -> forecast_adjustments.id
SCENARIO_ADJUSTMENT  -> forecast_scenario_assumptions.id (the assumption that generated it)
```

## Category resolution

Stage 1 data doesn't carry a ForecastCategory directly - it carries either
a CashEventType code (treasury_transactions.event_type_code) or a free-text
category string (expected_collections/expected_payments.category, set
manually or via Excel import). app/services/forecast_category_mapping.py
maps both onto the ForecastCategory hierarchy:

- EVENT_TYPE_TO_CATEGORY covers every CashEventType seeded in Stage 1
  (BANK_RECEIPT, SUPPLIER_PAYMENT, PAYROLL, TAX_PAYMENT, INTERCOMPANY_*,
  LOAN_*, INVESTMENT_*, BANK_FEE, ...).
- FREE_TEXT_CATEGORY_TO_FORECAST_CATEGORY covers common free-text values
  ("Trade Receivable", "Trade Payable", "Payroll", "Tax", "CAPEX", "Opex").
- Anything unmapped falls back to OTHER_INFLOW/OTHER_OUTFLOW rather than
  being silently dropped - the amount is never lost, only its category
  bucket is generic until the mapping is extended or the source data is
  corrected.

Both mappings are plain Python dicts, easy to extend without touching the
calculation engine itself.

## Reserved for future modules (interfaces only, not implemented)

SECTION 2/22/56 are explicit that these future sources must have a clean
place to plug in without a redesign, but must NOT be built yet:

| Future source | Reserved interface |
|---|---|
| Loan drawdowns/repayments | ForecastSourceType.FUTURE_LOAN; CashEventType codes LOAN_DRAWDOWN/LOAN_REPAYMENT/INTEREST_PAYMENT already exist and map to categories (LOAN_DRAWDOWNS, DEBT_PRINCIPAL, INTEREST) |
| Fixed deposit placements/maturities | ForecastSourceType.FUTURE_INVESTMENT; CashEventType codes INVESTMENT_PLACEMENT/INVESTMENT_MATURITY already exist and map to categories (INVESTMENT_PLACEMENTS, INVESTMENT_MATURITIES) |
| Intercompany reconciliation | ForecastSourceType.FUTURE_INTERCOMPANY; TreasuryTransaction.transfer_pair_id already links both legs of a transfer for a future matching engine to consume |
| Payments workflow | Once a Payments module exists, its cash-flow leg is simply a TreasuryTransaction with an appropriate event_type_code - no forecast-side change needed |
| Facilities (committed limits, overdraft headroom) | ForecastWeek.liquidity_available is currently just closing_cash; a Stage 4 Facilities module can extend the liquidity lookup to add undrawn facility headroom without changing the ForecastWeek schema |

None of these tables exist yet. When they're built, the pattern is: post
a TreasuryTransaction (or ExpectedCollection/ExpectedPayment, for
projected-not-yet-posted amounts) with the right event_type_code, and the
forecast engine picks it up automatically through the existing gathering
logic - no engine code change is anticipated to be required for the common
case of "a new kind of actual cash event." A genuinely new reserved
ForecastSourceType (FUTURE_LOAN etc.) would only be needed if that
module's data doesn't fit the TreasuryTransaction/Expected* shape at all.
