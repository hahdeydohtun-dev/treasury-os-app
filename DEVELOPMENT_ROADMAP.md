# Treasury OS — Development Roadmap

Per the build instructions, the application is developed in stages: the
foundation first, then one treasury module at a time, each independently
implementable without breaking the others.

## Stage 0 — Foundation (this delivery)

- [x] Repository structure (backend/frontend split, versioned API)
- [x] Architecture documentation (`ARCHITECTURE.md`)
- [x] Database foundation (PostgreSQL + Alembic, 11 tables migrated)
- [x] Authentication foundation (JWT access/refresh)
- [x] Group/entity foundation (`Group`, `LegalEntity`, `BusinessUnit`)
- [x] Currency foundation (`Currency`, versioned `FXRate`)
- [x] RBAC foundation (module × action × entity-scope permissions)
- [x] Audit foundation (append-only `AuditEvent` + `record_audit_event`)
- [x] API foundation (`/api/v1/*`, OpenAPI docs at `/api/v1/docs`)
- [x] Frontend shell (Next.js app shell, sidebar nav for all 19 modules,
      login page wired to the real API)
- [x] Automated tests (11 passing: security, RBAC rules, auth API,
      FX-rate versioning, health check)
- [x] Docker Compose for local Postgres/Redis
- [x] README / ARCHITECTURE / DOMAIN_MODEL / this roadmap

Explicitly **not** built yet: payments, reconciliation, investments, loans,
working capital, the 13-week forecast engine, KPIs, reports, tasks,
Excel Data Hub, AI Copilot.

## Stage 1 — Treasury Data Foundation + Excel Data Hub (delivered)

Combined the planned "Banks & Bank Accounts" and "Excel Data Hub" stages
into one delivery, since the Excel ingestion workflow needed bank
accounts to validate against, and bank accounts needed a real ingestion
path to be useful beyond manual entry.

- [x] `Bank`, `BankAccount` models (entity-scoped, currency-scoped,
      account-type-scoped via a configurable lookup table)
- [x] `TreasuryTransaction` — the common financial data layer every future
      module (payments, loans, investments, intercompany) will feed,
      instead of each module inventing its own transaction table
- [x] `BankBalance` (imported balances, duplicate-import-proof),
      `ExpectedCollection`, `ExpectedPayment`, `BankCharge`
- [x] Excel Data Hub: versioned template registry, upload → validate →
      duplicate-check → preview → confirm → import workflow, 7 templates
      (Bank Accounts, Bank Balances, Bank Transactions, Expected
      Collections, Expected Payments, FX Rates, Bank Charges)
- [x] Cash Position service/API (group/entity/currency/bank/account views)
- [x] Frontend: Cash & Liquidity, Banks & Accounts, Excel Data Hub screens
      wired to the real API
- [x] Entity-scoped RBAC enforced on every write path (banking, forecast
      inputs, uploads, imports), with a real bug found and fixed along the
      way (see ARCHITECTURE.md §12.2)
- [x] Audit logging on every create/upload/import
- [x] Demo dataset (2 entities, 2 banks, 3 accounts, multi-currency
      balances, an intercompany transfer pair, expected flows) clearly
      labeled `[DEMO]` / `DEMO_DATA`
- [x] 27 automated tests: banking API, Excel workflow end-to-end
      (structure validation, data validation, duplicate detection,
      import, entity-scope enforcement), cash position calculation,
      FX-rate-versioning-via-Excel, transaction audit logging
- [x] `TREASURY_DATA_MODEL.md`, `EXCEL_DATA_HUB.md` added; `ARCHITECTURE.md`,
      `DOMAIN_MODEL.md`, this roadmap updated

Explicitly **not** built yet (per the Stage 2 instructions): the full
13-week forecast engine, advanced liquidity optimization, loans/facilities,
payments workflow, bank/intercompany reconciliation matching engines,
fixed deposit lifecycle, working capital engine, risk engine, AI Copilot.
The data architecture is deliberately ready for all of them (SECTION 29).

## Stage 2 — 13-Week Cash Flow Forecast Engine (delivered)

Built exactly on the Stage 1 data foundation, per plan:

- Actual cash → `BankBalance`, `TreasuryTransaction`
- Expected collections/payments → `ExpectedCollection`, `ExpectedPayment`
- Bank charges → `BankCharge` (not yet wired into the waterfall as its
  own line source — currently only actuals/expected/recurring/manual
  adjustments feed lines; bank charges are visible via the Cash &
  Liquidity module but not yet a forecast category source)
- FX conversion → `FXRate` (reused as-is, no second FX system)
- Reserved interfaces (not implemented) for loan drawdowns/repayments,
  investment placements/maturities, and intercompany reconciliation — see
  `FORECAST_DATA_SOURCES.md`

Delivered:
- [x] Rolling 13-week horizon (7-day weeks, no calendar-month assumption),
      versioned via `Forecast.version`/`parent_forecast_id` — rolling
      forward creates a new row, never overwrites history
- [x] BASE/CONSERVATIVE/STRESS scenarios via configurable
      `ForecastScenarioAssumption` rows (collection delay, probability
      haircut, payment acceleration, unexpected outflow, min-cash-buffer
      multiplier) — no hard-coded percentages
- [x] GROSS vs PROBABILITY_ADJUSTED value basis, user-selected per forecast
- [x] Weekly waterfall: opening/closing cash, inflows/outflows/net
      transfers, minimum required liquidity, surplus/gap, liquidity
      coverage ratio — all backend-calculated with `Decimal` arithmetic
- [x] Entity-level and currency-level views computed live from
      `ForecastLine`, so a currency-specific shortfall is never hidden by
      a healthy consolidated figure (tested explicitly)
- [x] Manual forecast adjustments (additive layer, auditable, never
      touches source data) and recurring cash flow expansion
      (daily/weekly/biweekly/monthly/quarterly/custom)
- [x] Forecast lifecycle: DRAFT → PUBLISHED (immutable) → ARCHIVED, with
      recalculation blocked on a published forecast
- [x] What-if scenario workspace: a separate scratch forecast, never
      mutating the published one, with a week-by-week comparison result
- [x] Forecast-vs-actual variance and accuracy (derived, not persisted),
      configurable KPI target (not hard-coded at 5%)
- [x] Liquidity alerts (gap, low coverage, currency shortfall) generated
      at calculation time
- [x] Full REST API surface per the spec's endpoint list, entity-scoped
      RBAC on every route (reusing the existing `FORECAST_13WK` module —
      no RBAC schema changes needed)
- [x] Frontend dashboard: summary cards, closing-cash chart, weekly
      table, drill-down, alerts, calculate/publish controls
- [x] Demo data: liquidity thresholds, recurring payroll/opex, and a
      13-week expected cash-flow spread producing both a deliberate NGN
      liquidity gap (weeks 3–7) and a recovery into surplus (week 8+),
      verified live
- [x] 52 automated tests (dates, engine internals, scenarios/adjustments,
      full API lifecycle, authorization)
- [x] `FORECAST_ENGINE.md`, `FORECAST_METHODOLOGY.md`,
      `FORECAST_DATA_SOURCES.md` added; `ARCHITECTURE.md`,
      `DOMAIN_MODEL.md`, `README.md`, this roadmap updated

Known limitations carried forward (see `FORECAST_METHODOLOGY.md` and
`EXCEL_DATA_HUB.md`-style honesty): `liquidity_available` is just
projected closing cash (no facility/overdraft headroom yet — that's
Stage 3 below); bank charges aren't yet a forecast line source; transfer
sign is only known for `INTERCOMPANY_PAYMENT`/`INTERCOMPANY_RECEIPT`
event types, anything else classified `TRANSFER` is excluded from
`net_transfers` rather than guessed at.

## Stage 2 Hardening — Security & Quality Pass (delivered)

A dedicated hardening pass over Stages 0-2, focused on entity/group data
isolation. See `docs/STAGE_2_HARDENING_REPORT.md` for the full audit,
findings, fixes, and regression tests. Summary: replaced scattered
ad-hoc entity-permission checks with a centralized, group-aware
authorization module (`app/auth/authorization.py`); fixed a cross-group
data leak in `GROUP_WIDE` role assignments; converted list/aggregate/
export endpoints across banking, transactions, cash position, expected
collections/payments, bank charges, the Excel Data Hub, and the forecast
engine from "fetch broadly, hope the caller filtered" to server-side,
query-level entity scoping; closed a real gap where an Excel import's
row-level "Entity" column was never checked against the uploader's
authorization; added detail-endpoint authorization to several read
endpoints that had none. No new functionality was added — this stage adds
no new module scope beyond Stage 2.

## Stage 3 — Funding & Credit Facilities (delivered)

Built exactly as planned: Facility model with versioned terms/lifecycle
events (never overwrite original terms, per Principle 5 — same pattern as
`FXRate`), feeding into the 13-Week Forecast Engine via a clean adapter.
See `docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md` and
`docs/STAGE_3_IMPLEMENTATION_REPORT.md` for full detail.

- [x] Facility master with 11 configurable facility types, committed/
      uncommitted distinction, multi-entity, multi-currency
- [x] Facility lifecycle (enforced state machine) and versioning (every
      commercial-term change preserved, never overwritten)
- [x] Drawdowns (validated against availability, currency, dates,
      available capacity), repayments (partial/full/early, three
      amortization schedule methods), fees, covenants (never inventing a
      compliance value — `DATA_REQUIRED` until measured), collateral
      (haircut-adjusted eligible value)
- [x] Available capacity ≠ undrawn (covenant-restricted amounts and
      uncommitted-never-guaranteed both modeled explicitly)
- [x] Funding capacity/gap/cost analysis, all committed-vs-uncommitted
      kept strictly separate — never combined into one liquidity number
- [x] Forecast integration: facility repayments/interest/fees/approved
      drawdowns become real forecast lines with full source traceability,
      via one clean adapter — the Stage 2 forecast engine itself was not
      rewritten
- [x] Every endpoint uses the Stage 2 hardened authorization module — no
      second authorization system, verified via dedicated cross-entity/
      cross-group regression tests
- [x] 6 new Excel templates (Facility Master/Drawdowns/Repayments/Fees/
      Covenants/Collateral), reusing the existing generic ingestion
      engine, including row-level entity-authorization checks
- [x] Funding dashboard + facility list/detail frontend screens
- [x] Sub-limit-aware drawdown validation, full FundingAction workflow
      transitions (submit/review/approve/reject/execute/complete/cancel)
      with an explicit linkage to the underlying drawdown/repayment it
      authorizes, and a complete 11-tab facility detail page (Overview,
      Utilization, Versions, Drawdowns, Repayments, Fees, Covenants,
      Collateral, Sub-Limits, Events, Forecast Impact)
- [x] Production hardening: row-level locking (`SELECT ... FOR UPDATE`)
      on every shared financial balance, preventing concurrent overdraw
      of a facility's or sub-limit's capacity; idempotency guards on
      drawdown execution and repayment payment; repayment overpayment
      rejected outright
- [x] 120 automated tests (engine calculations, API integration, RBAC,
      forecast integration, concurrency, idempotency, versioning
      immutability, interest reproducibility, FundingAction data
      integrity) — full suite (Stage 0-3) at 120/120
- [x] FundingAction data integrity: mutual exclusivity of drawdown/
      repayment links, action-type-to-link consistency, entity/facility/
      amount/currency consistency against the linked transaction, one
      FundingAction per underlying transaction (app-level check plus a
      database-level partial unique index), and EXECUTED requiring the
      linked transaction's own status to show it actually happened (full
      `PAID`, not `PARTIALLY_PAID`, for a repayment link) — all enforced
      server-side and covered by dedicated cross-entity/cross-group tests
- [x] Repository hygiene: `backend/.env` removed from the repository,
      root-level `.gitignore` added, `.env.example` confirmed to contain
      only safe placeholders

Known limitations: automated covenant-vs-forecast projection is not
built; the frontend has no write-action buttons yet (view-only, so
nothing to audit for permission-hiding — backend authorization remains
authoritative regardless); a facility's outstanding balance at maturity
still requires an explicit scheduled `FacilityRepayment` to appear in the
forecast (by design — the system never invents one). See the Stage 3
doc's "Known limitations" section for full detail.

## Stage 4 — Investments (Fixed Deposits) (delivered)

Built exactly as planned, mirroring the Stage 3 Facility pattern:
Investment master with versioned terms (never overwritten), a separate
financial-transaction ledger (the master is never itself the cash
movement), an explicit approval-then-placement lifecycle, full/partial
early termination, rollover (creates a genuinely new investment with
explicit lineage) and rebooking (amends the same investment via a new
version), all feeding into the 13-Week Forecast Engine via a clean
adapter. See `docs/STAGE_4_INVESTMENTS.md` and
`docs/STAGE_4_IMPLEMENTATION_REPORT.md` for full detail.

- [x] Fixed Deposit fully implemented; 6 other instrument types seeded
      as configurable, inert lookup rows (no fake functionality)
- [x] Booking/maturity/rollover/early-termination lifecycle, with an
      enforced status-transition table and row-locked concurrency
      protection (verified with genuine concurrent requests: placement,
      partial termination, rollover)
- [x] Investment maturities (principal + interest) and planned
      placements feed the Forecast engine, with full source
      traceability and verified no duplication across recalculation
- [x] Investment liquidity view kept strictly separate from cash-at-bank
      figures; concentration reporting against configurable limits
- [x] Every endpoint uses the Stage 2 hardened authorization module
      (`TreasuryAction` extended with PLACE/TERMINATE/ROLLOVER/REBOOK) —
      no second authorization system, verified via dedicated
      cross-entity/cross-group regression tests
- [x] 5 new Excel templates (Investment Master/Placements/Interest/
      Terminations/Rollovers), reusing the existing generic ingestion
      engine, including row-level entity-authorization checks
- [x] Investments dashboard + detail (5-tab, read-only) frontend screens
- [x] 27 new automated tests (engine calculations, API integration,
      concurrency, cross-entity/cross-group RBAC, forecast integration,
      Excel auth, concentration) — full suite (Stage 0-4) at 143/143

Known limitations: no automatic interest-accrual posting job or
periodic-interest schedule generator; frontend has no write-action
buttons yet (backend fully functional regardless); no FX conversion for
a single cross-currency liquidity total. See the Stage 4 doc's "Known
limitations" section for full detail.

## Stage 5 — Bank Reconciliation

### Stage 5A — Bank Statement Ingestion & Normalization (delivered)

A normalized, bank-agnostic `BankStatementTransaction` evidence layer,
ingested through a new `BANK_STATEMENT` Excel Data Hub template with
zero changes to the shared upload/validate/confirm engine. Deliberately
NOT a second cash ledger — no effect on `TreasuryTransaction`,
`BankBalance`, or operational available cash. See
`docs/STAGE_5A_BANK_STATEMENT_INGESTION.md` for full detail.

- [x] `BankStatementTransaction` model, clearly distinct from
      `TreasuryTransaction`/`BankBalance`, with full source traceability
      (import batch + source row number)
- [x] Two-tier duplicate detection (exact vs. potential, backed by a
      partial unique index for strong-identity rows only) — never
      "same date + amount alone"
- [x] Statement-period overlap detection (informational, never a hard
      rejection — a reissued statement is not an automatic duplicate)
- [x] A real, pre-existing concurrency gap in the shared Excel import
      engine (`confirm_import` had no row lock) found and fixed — closes
      it for every template's import confirmation, not only bank
      statements, verified with genuine concurrent-request tests
- [x] Full entity/group RBAC via the existing authorization module,
      including a check that a row cannot reference another entity's
      real bank account even when the file's own Entity column claims
      otherwise
- [x] Multi-currency preserved exactly, never converted/merged
- [x] 43 new tests (model, full import lifecycle, duplicate detection,
      period overlap, cross-entity/cross-group security, traceability,
      multi-currency, idempotency, concurrency) — full suite (Stage 0-5A)
      at 192/192

Known limitations (all deliberately deferred): no matching/scoring
(5C), no open items/workflow (5D/5E), no reconciliation reports (5F), no
adaptive learning (5G). Corrected finding: Treasury OS has no
Tasks/Workflow system yet (`TASKS_WORKFLOW` is an unbuilt Stage 0 RBAC
placeholder, scheduled for Stage 7) — noted for future sub-stages that
will need to account for this rather than assume it exists.

### Stage 5B — Reconciliation Data Model (delivered)

The secure, auditable persistence foundation for reconciliation —
`ReconciliationRun`, `ReconciliationMatchSuggestion`,
`ReconciliationOpenItem`, `ReconciliationConfiguration` — with a
strictly separate run-creation vs. row-locked execution boundary. No
matching, scoring, or open-item workflow logic exists yet; execution
only counts in-scope bank statement evidence. See
`docs/STAGE_5B_RECONCILIATION_DATA_MODEL.md` for full detail.

- [x] `ReconciliationRun` scoped to exactly one entity + one bank
      account (never group-wide), with an explicit status-transition
      table mirroring `FACILITY_STATUS_TRANSITIONS`
- [x] `ReconciliationConfiguration` follows `FXRate`'s own effective-dated
      versioning discipline (version/is_current/superseded_by_id) — a
      run's `configuration_id` reference stays reproducible forever,
      even after the configuration is later superseded
- [x] Run creation and execution are strictly separate operations —
      creation never creates a suggestion or open item; execution only
      counts in-scope `BankStatementTransaction` rows and transitions
      status, verified to create zero cash-ledger side effects
- [x] Row-locked execution boundary (`SELECT ... FOR UPDATE`), verified
      with a genuine concurrent-request test — exactly one of two
      simultaneous executions succeeds
- [x] Full entity/group RBAC via the existing authorization module — zero
      new RBAC enum values needed (CREATE/VIEW/EDIT/EXECUTE/CLOSE/
      CONFIGURE all already existed)
- [x] 17 new tests (creation validation, cross-entity bank account
      rejection, full lifecycle, in-scope-only counting, non-ready
      execution rejection, zero side effects, configuration versioning,
      cross-entity/cross-group security, concurrency) — full suite
      (Stage 0-5B) at 209/209
- [x] Found and fixed a genuine timezone-column bug during testing
      (`started_at`/`completed_at`/`resolved_at` needed
      `DateTime(timezone=True)`) via a proper follow-up migration

### Stage 5C onward (not started)

- Deterministic matching engine (exact + tolerance + narration matching)
  over `TreasuryTransaction` rows against the Stage 5A evidence layer,
  writing real `ReconciliationMatchSuggestion` rows (Stage 5C)
- Advanced matching: one-to-many, many-to-one, batch payments, internal
  transfers, FX-aware matching (Stage 5D)
- Open-items-first UI (matched transactions available via drill-down only)
- Assignment/investigation/comment/resolution/approval/closure workflow

## Stage 6 — Intercompany Reconciliation

- Entity A ↔ Entity B matching engine, open-items-first UI, using
  `TreasuryTransaction.transfer_pair_id` as the starting point for
  identifying intercompany legs
- Group reporting consolidation of intercompany balances (eliminating the
  double-count the Stage 1 cash position foundation deliberately left
  unresolved — see ARCHITECTURE.md / TREASURY_DATA_MODEL.md)

## Stage 7 — Working Capital, Risk & Controls, KPIs, Reports, Tasks & Workflow

- Built once the underlying data (cash, forecast, facilities, investments,
  reconciliation) exists to report on / calculate against
- Reports generated from the deterministic engines, not authored manually

## Stage 8 — AI Treasury Copilot / Treasury Intelligence

Deliberately last among the deterministic-module stages: it
explains/summarizes/investigates/recommends over data and calculations
produced by the deterministic modules above. Any write action it proposes
goes through the same `require_permission` + `record_audit_event` path as
a human request (see ARCHITECTURE.md §7) — there is no separate
authorization path for AI-initiated changes.

## Stage 9 — Future API Integrations

Replaces the Excel Data Hub as the primary ingestion path for entities
whose bank/ERP/payment platforms expose a real API, without changing the
Treasury Data Model or any downstream engine — per the architecture
principle established from the start (SECTION 2/29 of the Stage 1 spec):
Excel is one ingestion adapter among several, never the schema's design
center.

## Cross-cutting, ongoing throughout every stage

- RBAC: add `Permission` grants for each new module/action combination as
  it's built; no schema change needed (see ARCHITECTURE.md §5)
- Audit: every material write calls `record_audit_event` in the same
  transaction as the change
- Tests: unit tests for business rules, integration tests for the API,
  DB tests for anything with lifecycle/versioning semantics
- Never hard-code financial values or currencies; never present mock data
  as real without an explicit "DEMO DATA" label
- New Excel templates are one `TemplateSpec` entry in
  `app/services/excel_templates.py`, not a rewrite of the ingestion engine
