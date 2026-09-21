# Treasury Data Model

This document describes the common financial data layer added in Stage 1
(Treasury Data Foundation + Excel Data Hub), and the design decisions
behind it. It complements `DOMAIN_MODEL.md` (full column-level reference
for every table) - this file focuses on why the model looks the way it
does and how future modules are expected to use it.

## The problem this solves

Payments, bank transactions, customer collections, loan drawdowns,
investment maturities, intercompany transfers, and bank charges all,
fundamentally, describe the same thing: money moving (or about to move)
for a legal entity, in some currency, on some date, from or to some
counterparty. Building a separate table per module produces N incompatible
shapes and makes cross-module reporting (a cash position, a forecast) a
data-integration project instead of a query.

Stage 1 instead defines one generic model - `TreasuryTransaction` - for
every actual cash event, classified by an extensible `event_type_code`
rather than a hard-coded type per module.

## Layered architecture

```
EXTERNAL SOURCE  (Excel today; ERP/Bank/Payment/Accounting/Open Banking APIs later)
      v
DATA INGESTION   (Excel Data Hub - see EXCEL_DATA_HUB.md)
      v
VALIDATION       (per-template row validators)
      v
NORMALIZATION    (importer functions map source columns to domain fields)
      v
TREASURY DATA MODEL   (this document)
      v
TREASURY ENGINES      (Cash Position today; 13-Week Forecast, Liquidity,
                        Reconciliation next)
      v
REPORTING / AI
```

Whatever module is added next (Payments, Loans, Reconciliation, ...), it
either:

1. Writes a `TreasuryTransaction` directly for its cash-flow leg
   (e.g. a Loan Repayment posts a `LOAN_REPAYMENT` transaction), or
2. Owns its own specialized table for module-specific state (e.g. a
   future `Facility` table holding committed limits, covenants, repayment
   schedules) and references `TreasuryTransaction` rows for the actual
   cash movements that table's lifecycle produces.

Nothing about the database is designed around Excel's column layout -
Excel is one ingestion adapter among several the model is built to accept
(SECTION 2/29 of the Stage 1 spec).

## Core model: TreasuryTransaction

Every event carries:

- Identity & context: `legal_entity_id`, `business_unit_id` (optional),
  `event_type_code`, `direction`.
- Dates: `event_date`, `value_date`, `posting_date` - kept distinct
  because a bank transaction's value date, the date it's economically
  effective, and the date it's posted to the ledger routinely differ.
- Multi-currency shape: `transaction_currency_code`/`amount`,
  `functional_currency_code`/`amount`, `reporting_currency_code`/`amount`,
  plus `exchange_rate`/`exchange_rate_type`/`exchange_rate_date`/
  `exchange_rate_source`. This mirrors the shape the product spec requires
  on every financial transaction, and lines up with how `FXRate` already
  represents rates (same `rate_type` vocabulary: SPOT/BUDGET/MANAGEMENT/
  TREASURY/HISTORICAL).
- Bank linkage (optional): `bank_id`, `bank_account_id`. Optional
  because not every cash event is bank-specific (e.g. an accrual or a
  non-cash FX revaluation entry, classified NON_CASH).
- Transfer pairing: `transfer_pair_id` groups the two legs of an
  internal transfer (source account leg + destination account leg) so a
  future consolidation step can identify and eliminate them from group
  totals without double-counting. See "Group Consolidation" below.
- Source traceability: `source_type`, `source_file`,
  `source_record_id`, `import_batch_id` - every imported row can answer
  "where did this come from?" (SECTION 19).
- Status: PENDING/POSTED/CANCELLED. Correcting a POSTED transaction is
  a status change plus a new offsetting/corrected transaction, never an
  in-place edit of the amount - the same "never silently overwrite
  history" principle already applied to `FXRate`.

### Cash event classification

Every transaction resolves to one of:

- INFLOW - cash coming in (customer collection, loan drawdown, ...)
- OUTFLOW - cash going out (supplier payment, tax payment, ...)
- TRANSFER - moving between the group's own accounts (see below)
- NON_CASH - an event worth recording that isn't itself a cash
  movement (e.g. an FX revaluation)

`CashEventType.default_direction` seeds this on new transactions, but the
direction is stored per-transaction (not just derived at read time), so an
edge case can be classified correctly without contorting the event type
taxonomy.

### Configurable lookups, not enums

`AccountType` (bank account types) and `CashEventType` (the BANK_RECEIPT /
SUPPLIER_PAYMENT / ... list from SECTION 4) are database tables, not
Python enums, following the same pattern as `Currency`. New types are an
API call, never a code change - currently seeded via
`app/db/seed_reference_data.py` and extendable the same way `Currency` is.
`CashDirection` (INFLOW/OUTFLOW/TRANSFER/NON_CASH) is the one true enum
here, because the spec fixes that classification set explicitly
(SECTION 6) and future event types are expected to map onto it, not
extend it.

## Forecast-input tables

`ExpectedCollection` and `ExpectedPayment` are not `TreasuryTransaction`
rows. They represent money that hasn't moved yet and may not (hence
`probability`, `status`), which is a materially different lifecycle from a
posted cash event. Keeping them separate means:

- The Cash Position service (actuals only) never accidentally counts an
  expectation as cash.
- The future 13-Week Forecast Engine can read expected flows and actual
  flows as two distinct, clearly-labeled inputs, exactly as SECTION 29
  requires (actual cash, expected collections, and expected payments are
  named as separate forecast inputs).

`BankCharge` is deliberately its own small table rather than a
`TreasuryTransaction` subtype, since it's typically imported in bulk from
a dedicated bank charges statement and doesn't need the full transaction
shape (no functional/reporting currency conversion is modeled for it at
this stage) - but nothing prevents promoting bank charges into
`TreasuryTransaction` rows (event_type BANK_FEE) in a later stage if
reporting needs that.

## Bank & Bank Account model

`Bank` is group-level shared master data - one row per real-world
banking institution, not duplicated per entity. `BankAccount` is where
entity/currency/account-type scoping actually lives:
`legal_entity_id` + `bank_id` + `currency_code` + `account_type_code`,
plus operational fields (`minimum_operating_balance`, `overdraft_limit`,
`status`, opening/closing dates). Account numbers are stored in full (for
future reconciliation matching) but every API response masks them
(`BankAccountOut.account_number_masked`) - the raw number is never
returned or logged.

## Cash Position

`app/services/cash_position_service.py` reads the single most recent
`BankBalance` on or before an as-of date, per matching `BankAccount`, and
sums by group/entity/currency/bank/account. This is deliberately the
simplest thing that could work for "what's our cash right now" - no
funding optimization, no liquidity coverage ratio, no facility-aware
overdraft netting. Those belong to the future Cash & Liquidity Engine
proper. A second function, `calculate_net_movement`, sums POSTED
`TreasuryTransaction` amounts by direction over a date range - a
movement-based cross-check against the balance-based figure, useful once
both bank-balance imports and bank-transaction imports exist for the same
account.

Multi-currency caveat: the current API sums `total_cash` across
currencies without conversion - it's a display convenience, not a real
consolidated figure. The `by_currency` breakdown is the number to trust
until an FX-aware consolidation step exists.

## Group Consolidation (foundation only)

SECTION 21 of the Stage 1 spec explicitly asks for "the correct foundation"
for eliminating intra-group transfers from group-level totals, not a full
consolidation engine. Today:

- A transfer's two legs (source account debit, destination account
  credit) share a `transfer_pair_id`.
- Both legs classify as `direction = TRANSFER`, so they're already
  excluded from INFLOW/OUTFLOW aggregation in `calculate_net_movement`.
- Cash Position sums balances, not movements, so transfers between two
  group accounts already don't double-count group cash there (each
  account's balance reflects the transfer once).

What's not built: automatic detection that two TRANSFER transactions
are a matched pair (vs. a genuine leg missing its counterpart), and
group-level P&L/balance-sheet consolidation. That's Stage 6
(Intercompany Reconciliation) in DEVELOPMENT_ROADMAP.md.

## What every future module should do

When you build the next module (Payments, Facilities, Reconciliation,
...):

1. Don't invent a new transaction table. Post a `TreasuryTransaction`
   with the right `event_type_code` for the module's cash-flow leg.
2. Do add module-specific state in its own table (a `Facility`, a
   `FixedDeposit`, ...) when there's lifecycle/versioning detail that
   doesn't belong on a generic transaction row.
3. Reuse `FXRate` and `Currency` for any conversion - don't build a
   second FX table.
4. Reuse the `import_batch_id` / `source_type` / `source_record_id`
   traceability fields rather than inventing new ones.
5. Add new `TreasuryModule`/`TreasuryAction` enum values as needed
   for RBAC (they're already extensible per-module).
