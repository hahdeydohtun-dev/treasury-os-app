# Stage 4 Implementation Report - Investments & Fixed Deposit Management

## 1. Files changed

New: app/models/investment.py, app/services/investment_engine.py,
app/services/investment_validation_service.py,
app/services/investment_service.py,
app/services/investment_forecast_adapter.py,
app/services/investment_excel_templates.py, app/schemas/investment.py,
app/api/v1/investments.py, app/api/v1/investment_reports.py,
app/db/seed_investment_types.py, 4 new test files, 4 new migrations.
Modified: app/models/__init__.py (registered new models),
app/models/forecast.py (ForecastSourceType +5 values),
app/models/rbac.py (TreasuryAction +4 values),
app/services/forecast_engine.py (+1 gathering call),
app/services/excel_templates.py (registry merge),
app/api/v1/router.py (2 new routers registered), tests/conftest.py
(investment_types fixture, truncate list). Frontend: lib/api-client.ts
(investment types/methods), lib/nav-items.ts (enabled Investments),
app/investments/page.tsx (new), app/investments/[id]/page.tsx (new).

## 2. Database models

InvestmentType (configurable lookup), Investment (master record),
InvestmentVersion (versioned terms), InvestmentTransaction (the
financial-event ledger - placement/interest/termination/rollover/
rebooking/penalty/maturity settlement), InvestmentEvent (append-only
timeline), InvestmentConcentrationLimit (configurable limits). 6 new
tables total.

## 3. Migration

Four new migrations, none modifying a previously-applied one:
1. stage4 investments and fixed deposit management - the 6 tables above.
   (Autogenerate incorrectly proposed dropping Stage 3's hand-written
   partial unique indexes on funding_actions, since SQLAlchemy's model
   metadata doesn't reflect them - those lines were removed from both
   upgrade() and downgrade() before applying.)
2. add funding action linked drawdown and repayment fk - N/A, this was
   from the prior Stage 3 pass, listed here only for chain continuity.
3. add place terminate rollover rebook treasury actions - ALTER TYPE
   ADD VALUE on the existing treasuryaction enum (4 new values).
4. add investment forecast source types - ALTER TYPE ADD VALUE on the
   existing forecastsourcetype enum (5 new values). This one was added
   after discovering a genuine bug during testing: the Python
   ForecastSourceType enum was extended but the corresponding Postgres
   enum migration was initially missed, causing a runtime
   InvalidTextRepresentationError the first time an investment forecast
   line was actually persisted. Caught by
   test_investment_maturity_appears_in_forecast_with_traceability_and_no_duplication,
   fixed with this migration, and re-verified.

`alembic heads` returns exactly one head after all four:
`571ec4b77c92`. Verified against a completely fresh database (`CREATE
DATABASE` -> `alembic upgrade head`): all 12 migrations in the full
chain (Stage 0 through this pass) applied without error, resulting in
49 tables.

## 4. APIs

`app/api/v1/investments.py`: investment-types list, create, list
(entity-scoped), get, update (DRAFT only), status transition, place,
terminate, rollover, rebook, compare-rollover, versions, events,
transactions. `app/api/v1/investment_reports.py`: liquidity,
maturity-calendar, concentration, alerts. 18 new endpoints (route count
120 -> 138 after registration).

## 5. Frontend screens

`/investments` - liquidity summary cards, alerts panel, investment
register table. `/investments/[id]` - 5-tab detail page (Overview,
Commercial Terms, Versions, Transactions, Events). Both read-only (no
write-action buttons yet - documented as a known limitation; all backend
endpoints are fully functional regardless).

## 6. Workflow

DRAFT -> SUBMITTED -> UNDER_REVIEW -> APPROVED -> PLACEMENT_PENDING ->
ACTIVE, enforced by an explicit `INVESTMENT_STATUS_TRANSITIONS` table
(same pattern as Stage 3's `FACILITY_STATUS_TRANSITIONS`). Creation
never places an investment; placement is a separate, explicit,
idempotent action.

## 7. Interest engine

`calculate_simple_interest`: principal x rate x day-count-fraction,
supporting ACT/365, ACT/360, 30/360 - a pure function, Decimal
throughout, verified reproducible after later amendments.

## 8. Termination logic

`calculate_early_termination`: principal returned, interest earned to
the termination date (never a to-maturity figure), penalty (4 types),
net proceeds. Full and partial termination both supported and tested;
concurrent partial terminations cannot together exceed outstanding
principal (row-locked re-validation).

## 9. Rollover/rebooking logic

Rollover creates a genuinely new `Investment` row with explicit
`previous_investment_id`/`rolled_to_investment_id` lineage - the
original's terms are never overwritten. Rebooking amends the same
investment via a new `InvestmentVersion`. `compare-rollover` returns
transparent comparison factors only, never a ranked/scored "best"
option.

## 10. Forecast integration

`investment_forecast_adapter.py` feeds scheduled maturities (principal +
interest, separately traceable) and planned future placements into the
existing forecast engine via one new gathering call. Verified: no
duplication across repeated recalculation; full source traceability
back to the originating investment id.

## 11. Liquidity integration

`GET /investments-reports/liquidity` reports invested-funds figures only
(total invested, maturing in 7/30/90 days, expected interest, weighted
average rate, broken down by currency/entity/institution) - never
combined with or presented as available cash.

## 12. Concentration

`GET /investments-reports/concentration` reports exposure by
institution/currency/investment-type against configurable
`InvestmentConcentrationLimit` rows, with WITHIN_LIMIT/WARNING/BREACH
status - verified with a real breach scenario.

## 13. Excel Data Hub

5 new templates (Investment Master/Placements/Interest/Terminations/
Rollovers), reusing the existing generic ingestion engine, including
row-level entity-authorization checks - verified live and by test.

## 14. RBAC/security

Every endpoint uses the Stage 2 hardened authorization module.
`TreasuryAction` extended with PLACE/TERMINATE/ROLLOVER/REBOOK.
Extensively tested: cross-entity isolation (create/view/list/terminate/
liquidity all correctly blocked), cross-group isolation, direct-ID
access attempts.

## 15. Audit

Every material action (create, edit, status change, place, terminate,
rollover, rebook) writes both an `InvestmentEvent` (business timeline)
and an `AuditEvent` (system-wide audit trail) with who/what/when/entity/
investment/reason/before/after.

## 16. Concurrency/idempotency

`SELECT ... FOR UPDATE` row locking on every endpoint that mutates a
shared balance or status. Verified with genuine concurrent requests
(`asyncio.gather`): concurrent placement (exactly one succeeds, one
PLACEMENT transaction), concurrent partial termination (outstanding
principal never over-terminated), concurrent rollover (exactly one
replacement investment ever created).

## 17. Tests added

27 new tests across 4 files:
- `test_investment_engine.py` (7): day-count conventions, purity/
  reproducibility, termination with each penalty type, rollover
  comparison never ranks.
- `test_investment_api.py` (6): creation with correct expected-interest,
  illegal transition rejected, idempotent placement, partial-then-full
  termination, rollover preserves original, rebooking creates new
  version.
- `test_investment_production_hardening.py` (10): concurrent partial
  termination, concurrent placement, concurrent rollover, version
  immutability, forecast integration + no duplication, cross-entity
  isolation, cross-group isolation, Excel row-level auth, concentration
  breach, currency preservation.
- Plus a new `investment_types` fixture and truncate-list update in
  `tests/conftest.py`.

## 18. Complete test result

Exact command:
```bash
cd backend && source .venv/bin/activate && python -m pytest -q
```
**Collected: 143. Passed: 143. Failed: 0. Skipped: 0. Exit code: 0.**
(120 prior Stage 0-3 + 23 new Stage 4.) Actually executed, rerun after
every code change in this pass, always 143/143 at completion.

## 19. Ruff result

```bash
ruff check app/ tests/
```
All genuine issues auto-fixed (import ordering, one nested-if
simplification fixed manually). Remaining findings are exclusively
`RUF059` (unused unpacked tuple elements in test destructuring, e.g.
`group, entity_a, _ = demo_group_and_entities`) - the same stylistic
pattern already present and accepted throughout the pre-existing Stage
2/3 test suite, not a defect.

## 20. Mypy result

```bash
mypy app/models/investment.py app/services/investment_engine.py \
     app/services/investment_validation_service.py \
     app/services/investment_service.py \
     app/services/investment_forecast_adapter.py \
     app/api/v1/investments.py app/api/v1/investment_reports.py \
     --ignore-missing-imports
```
**0 findings** on the core Stage 4 logic (models, engine, validation,
orchestration, forecast adapter, both API routers) after fixing 2
genuine issues (a missing None-guard in the concentration-limit lookup,
and a raw-ORM-object-passed-where-schema-expected type mismatch in the
termination endpoint).

`app/services/investment_excel_templates.py` has the same class of
`union-attr` findings (an `import_row` function using a record that
`validate_row` already confirmed exists, without re-narrowing the
Optional type) as the pre-existing, already-accepted
`facility_excel_templates.py` - confirmed by running mypy against that
Stage 3 file too and finding the identical pattern already present
there. Not a new defect; consistent with established codebase
convention.

## 21. Frontend build result

Exact command:
```bash
cd frontend && npm install --no-audit --no-fund && npm run build
```
`✓ Compiled successfully`, `✓ Generating static pages (12/12)`, exit
code 0. Routes: `/`, `/_not-found`, `/banks-accounts`, `/cash-liquidity`,
`/dashboard`, `/excel-data-hub`, `/facilities`, `/facilities/[id]`,
`/forecast`, `/investments` (new), `/investments/[id]` (new), `/login`.

## 22. Alembic result

```bash
alembic heads
```
**Exactly one head: `571ec4b77c92`.** Fresh-database migration test
(`CREATE DATABASE` -> `alembic upgrade head`): all 12 migrations applied
in order with zero errors, 49 tables resulting. Scratch database dropped
after verification.

## 23. Known limitations

- No automatic interest-accrual posting job (`INTEREST_ACCRUAL`
  transactions are not generated on a schedule).
- No automatic `PERIODIC` interest-payment schedule generator.
- Frontend is read-only (dashboard, list, detail) - no write-action
  buttons; every backend action is fully functional and
  authorization-checked via the API/tests regardless.
- `compare-rollover` has no dedicated frontend screen (API-only).
- No FX conversion for a single cross-currency liquidity total - each
  currency's principal is kept distinct, per the "never silently
  convert historical amounts" instruction.

## 24. Confirmation: Stage 0-3 remain intact

All Stage 0-3 test files (facility, funding action, forecast, security,
excel data hub, transactions/audit, FX rate versioning, banking, cash
position, auth) ran in the same 143-test suite as every new Stage 4
test, all passing together. No Stage 0-3 test was modified, skipped, or
removed. The only Stage 3 artifact touched was the migration chain
itself (extending it, never editing an applied migration) and the
`TreasuryAction`/`ForecastSourceType` enums (extended additively via
`ALTER TYPE ADD VALUE`, existing values untouched) - both verified not
to break any existing Stage 3 test.

## 25. Confirmation: Stage 5 was NOT implemented

No Bank Reconciliation, Intercompany Reconciliation, AI Treasury
Copilot, external bank API, or open-banking code was created in this
pass. No investment instrument beyond Fixed Deposit has working
placement/termination/rollover logic - the other investment types exist
only as inert lookup rows (`is_implemented=False`), exactly as SECTION 3
and SECTION 45 require.

## Addendum — Financial Integrity Hardening (cash/treasury integration)

### Architecture inspection (before any changes)

1. Cash movement model: `TreasuryTransaction` (app/models/treasury_transaction.py)
   — the single ledger every module feeds via `event_type_code`/
   `source_type`/`source_record_id`.
2. Available-cash calculation: `cash_position_service.calculate_cash_position`
   — derived from the latest reported `BankBalance` per account.
3. Forecast feed: `forecast_engine.py` + the existing adapter pattern
   (unchanged).
4. `InvestmentTransaction` ↔ cash: new `cash_transaction_id` FK.
5. Authorization: `app.auth.authorization` (unchanged, already correctly
   reused throughout Stage 4).

`CashEventType` already had `INVESTMENT_PLACEMENT`/`INVESTMENT_MATURITY`
pre-seeded from Stage 1 — confirming this integration was always
intended, not newly invented.

### Files changed

`app/models/investment.py` (`cash_transaction_id` FK + docstring),
`app/services/investment_service.py` (rewritten: real cash creation in
placement/termination/rollover/rebooking, new `settle_maturity`,
`recompute_expected_interest`), `app/services/investment_forecast_adapter.py`
(periodic-interest exclusion fix), `app/api/v1/investments.py` (cash-
position-based placement validation, currency/entity checks, new
settle-maturity endpoint), `app/schemas/investment.py` (new/extended
request schemas), `app/db/seed_reference_data.py` (3 new `CashEventType`
rows), `tests/conftest.py` (`cash_event_types` fixture), one new test
file (`test_investment_cash_integration.py`, 15 tests), one new
migration, `README.md` (full rewrite), `docs/STAGE_4_INVESTMENTS.md`
(sections 6, 14-17, 20, 27 updated).

### Database migrations added

One new migration: `InvestmentTransaction.cash_transaction_id` FK to
`treasury_transactions`, plus `ux_investment_transactions_one_placement`
(a partial unique index — at most one PLACEMENT transaction per
investment, database-level defense in depth alongside the existing row
locking). `alembic heads` → exactly one head (`146d9a612050`). Fresh-
database test: all 13 migrations (Stage 0→4) applied cleanly, 49 tables,
Stage 3's `funding_actions` integrity constraints confirmed untouched.

### Cash integration implemented

- **Placement**: creates a real `OUTFLOW` `TreasuryTransaction`, linked
  via `cash_transaction_id`. Authoritative available cash is computed
  server-side from `calculate_cash_position` (client-supplied
  `available_cash` is accepted only for display, never the decision).
  Currency and entity ownership of the source account are validated.
- **Termination**: creates one `INFLOW` reconciling EXACTLY to
  `net_proceeds` (never principal or interest alone); a separate
  `PENALTY` `InvestmentTransaction` row exists for the component
  breakdown but is not a second cash movement.
- **Maturity settlement**: new explicit `POST
  /investments/{id}/settle-maturity` — creates one `INFLOW` for
  principal + interest; idempotent.
- **Rollover**: creates exactly ONE `NON_CASH` `TreasuryTransaction`
  shared by both legs — never two offsetting real cash movements.
  `NON_CASH` rows are already excluded from forecast/cash-position
  calculations elsewhere in the codebase.
- **Rebooking**: additional principal creates a real `OUTFLOW`,
  identical treatment to placement.

### Investment lifecycle changes

`expected_interest` is now recomputed (`recompute_expected_interest`)
after every event that changes outstanding principal, rate, or maturity
date — termination, rollover (on the remaining original), rebooking,
and rate/maturity-date PATCH. Historical `InvestmentVersion` rows are
never touched by this recomputation.

### Forecast changes

`investment_forecast_adapter.py` now only generates the maturity-
interest line for `AT_MATURITY` investments — `PERIODIC`/`UPFRONT`
investments' interest is excluded (not misrepresented) until a proper
periodic-schedule generator exists. The principal line is unaffected.

### RBAC/security changes

No new authorization mechanism. Placement/rebooking now additionally
validate that the funding bank account belongs to the investment's own
legal entity and matches its currency — closing a gap where an account
belonging to a different entity could otherwise have been referenced.

### Idempotency/concurrency changes

Placement's existing row-lock + application check now has a database-
level backstop (`ux_investment_transactions_one_placement`). Maturity
settlement is a new idempotent action (checked both by the investment's
status guard and, redundantly, inside `settle_maturity` itself).
Existing termination/rollover concurrency protection (Stage 4's
original hardening) is unchanged and still verified.

### Tests added

15 new tests in `tests/test_investment_cash_integration.py`: placement
creates a real linked cash transaction; insufficient cash rejected with
no mutation; client-supplied available_cash cannot bypass the real
check; currency mismatch rejected; cross-entity account use rejected;
termination cash inflow reconciles exactly to net proceeds; partial
termination recomputes expected interest correctly; full rollover
creates no external cash movement (only one NON_CASH record); partial
rollover principal split correct; rebooking creates a real cash outflow;
rebooking rejected when cash insufficient; maturity settlement explicit
and idempotent; original principal invariant across termination;
outstanding-principal-never-negative invariant; periodic-interest
investment excluded from the maturity-interest forecast line.

### Complete test result

```bash
cd backend && source .venv/bin/activate && python -m pytest -q
```
**Collected: 158. Passed: 158. Failed: 0. Skipped: 0. Exit code: 0.**
(143 prior + 15 new.) Actually executed; two transient Postgres-
connection drops during this session were infrastructure hiccups
(service not started), not code failures — resolved by restarting
Postgres and rerunning, with the rerun result reported above.

### Static check result

`ruff check app/ tests/` — all genuine issues auto-fixed; remaining
findings are exclusively the pre-existing `RUF059` stylistic pattern
already accepted throughout the codebase.
`mypy` on every file touched in this pass (models/investment.py,
services/investment_service.py, services/investment_engine.py,
services/investment_forecast_adapter.py, api/v1/investments.py,
schemas/investment.py) — **0 findings.**

### Frontend build result

```bash
cd frontend && npm install --no-audit --no-fund && npm run build
```
`✓ Compiled successfully`, `✓ Generating static pages (12/12)`, exit
code 0. No frontend files were changed in this pass (backend/cash-
integration focused); this confirms the build still succeeds unchanged.

### Fresh database migration result

`CREATE DATABASE` → `alembic upgrade head`: all 13 migrations applied in
order, 49 tables, zero errors. Scratch database dropped after
verification.

### Alembic head result

Exactly one head: `146d9a612050`.

### README/.gitignore status

`README.md` rewritten in full — was stale at "Stage 0 foundation only";
now accurately states Stage 0-4 Complete / Stage 5 Not Started, with new
architecture, investment-lifecycle, cash-integration, and multi-entity/
currency sections. `.gitignore` reviewed against this task's required
minimum list — already fully compliant, no changes needed.

### Known limitations (see docs/STAGE_4_INVESTMENTS.md section 27 for full detail)

No automatic interest-accrual posting job or periodic-interest schedule
generator (interest for `PERIODIC`/`UPFRONT` investments is now
correctly excluded from the forecast rather than misrepresented); no
FX conversion for a single cross-currency liquidity total; frontend
remains read-only for write actions (backend fully functional
regardless); cash-sufficiency checks reflect the latest reported
`BankBalance` snapshot, not a real-time running balance — a pre-existing
Treasury OS architectural characteristic shared by every cash-affecting
operation in the codebase, not unique to Stage 4, and documented rather
than worked around with a second cash ledger.

### Confirmation: Stage 0-3 remain intact

The full 158-test suite includes every Stage 0-3 test file, all passing
together in the same run. The only Stage 3 artifacts touched were the
migration chain (extended, never edited) and, in the prior Stage 4 pass,
the `TreasuryAction`/`ForecastSourceType` enums (extended additively) —
both re-verified not to break any Stage 3 test in this pass too.

### Confirmation: Stage 5 was NOT started

No Bank Reconciliation, Intercompany Reconciliation, AI Copilot, or
external bank API code was created or modified in this pass. No actual
bank payment execution was implemented anywhere — placement,
termination, and maturity settlement all remain internal Treasury OS
representations of financial state, never a real external transfer.

## Addendum 2 — Final Financial-Integrity Patch (operational cash availability)

### Architecture inspection (before any changes)

Confirmed `calculate_cash_position()` reads only `BankBalance`, never
incorporating posted `TreasuryTransaction` movements — exactly the bug
described. Confirmed facilities (`app/api/v1/facilities.py`,
`app/api/v1/funding.py`) do not call `cash_position_service` at all
(they use `FacilityEngine`'s own `committed_limit`/`current_drawn_amount`
concept, unrelated to bank cash) — zero regression risk there.

### Files changed

`app/services/cash_position_service.py` (new
`calculate_operational_available_cash` function + `OperationalCashPositionResult`
dataclass — `calculate_cash_position` itself left completely unchanged),
`app/services/investment_engine.py` (`generate_periodic_interest_schedule`),
`app/services/investment_service.py` (idempotency-key check in
`rebook_investment`), `app/services/investment_forecast_adapter.py`
(rewritten: periodic/upfront interest gathering, independent of the
maturity-in-horizon filter), `app/api/v1/investments.py` (placement and
rebooking now call `calculate_operational_available_cash`), `app/models/investment.py`
(`idempotency_key` column), `app/schemas/investment.py`
(`RebookingRequest.idempotency_key`), 2 new migrations, ~20 new tests in
`tests/test_investment_cash_integration.py`, one existing test renamed
and reworked to test actual cash-position behavior (section 9).

### Database migrations added

Two: (1) no schema change of its own — verified alongside the
idempotency migration; (2) `InvestmentTransaction.idempotency_key`
column + `ux_investment_transactions_idempotency_key` partial unique
index on `(investment_id, idempotency_key)`. `alembic heads` → exactly
one head (`c68cb5de2aea`). Fresh-database test: all 14 migrations
(Stage 0→4, including both this pass's) applied cleanly, 49 tables.

### Operational cash architecture (C)

```
operational_available_cash
    = reported_available_balance         (A: latest BankBalance, anchored
                                              to its own balance_date)
    + unreflected_inflows                (C: posted TreasuryTransaction
    - unreflected_outflows                   rows on this account dated
                                              AFTER that balance_date)
```

`BankBalance` is never mutated. `TreasuryTransaction` remains the only
transaction table. No second cash ledger was created. Double-counting
after a later balance import is prevented by the anchor mechanism: a
transaction only counts as "unreflected" while dated after the latest
reported balance's own date — once a newer balance catches up past that
transaction's date, it naturally drops out of the sum.

### Placement behavior (D)

Now uses `calculate_operational_available_cash` (not the raw reported
balance) as the authoritative check. Verified: 100m reported → 60m
placed → operational cash correctly 40m → a second 50m placement
rejected → a second 40m placement (exactly what's left) succeeds → final
operational cash exactly 0.

### Termination/maturity behavior (E/F)

Unchanged in mechanism from the prior pass (net-proceeds-exact INFLOW,
explicit idempotent maturity settlement) — now additionally verified to
correctly increase `operational_available_cash` by the exact net
proceeds / principal+interest amount.

### Rollover behavior (G)

Unchanged in mechanism (one NON_CASH record, never two offsetting real
movements) — now additionally verified that `operational_available_cash`
is bit-for-bit unchanged across a full rollover.

### Rebooking behavior (H) — idempotency

New `idempotency_key` (optional, client-supplied) on `RebookingRequest`.
A retry with the same key against the same investment returns the
investment unchanged, no second cash movement — backed by both an
application-level pre-check (safe under the investment's own row lock,
which the caller already holds for the whole call) and a database-level
partial unique index. A different key still applies a genuine second
rebooking normally.

### Periodic interest (I)

`generate_periodic_interest_schedule` produces real MONTHLY/QUARTERLY/
SEMI_ANNUAL/ANNUAL payment dates. **A real bug was found and fixed
during testing**: the first implementation nested periodic-interest
gathering inside the maturity-in-horizon filter, so payments within a
forecast's horizon were silently dropped whenever the investment's own
maturity fell beyond that (always-fixed 13-week) horizon. Fixed by
gathering periodic interest independently of the maturity filter.
UPFRONT interest now appears once, at the placement date, only while
still pending. AT_MATURITY unchanged. CUSTOM frequency remains
undocumented/deferred (no stored dates to build from) — explicitly
stated, not silently approximated. `forecast_engine.py` itself was not
touched — all investment-specific logic lives in
`investment_forecast_adapter.py` / `investment_engine.py`, per the
instruction.

### Forecast behavior (J)

Every investment-derived forecast line still carries
`source_type`/`source_id`/`legal_entity_id`/`currency_code`/`event_date`/
`amount`/`direction` (via the existing `ForecastLine` schema — no new
concept introduced). Periodic interest lines use
`{investment_id}:period-{n}` as `source_id` for per-period traceability.
Forecast generation remains idempotent (the existing
delete-then-regenerate pattern in `forecast_engine.py`, unchanged).

### Double-count protection (K)

See "Operational cash architecture" above. Verified by the exact
scenario in the task: 100m → 60m placement → operational cash 40m →
later balance import reports 40m directly → operational cash correctly
reads 40m, never -20m.

### RBAC/security (L)

Re-verified: cross-entity investment/account/cash tests all still pass
(15 cross-entity/cross-group tests explicitly re-run). Multi-currency:
a new test confirms the operational-cash check operates on the specific
account's own currency, never a blended/aggregated figure across
currencies.

### Idempotency/concurrency (M)

Placement idempotency (DB constraint + row lock, prior pass) unchanged
and re-verified. Rebooking idempotency is new this pass (see above).

### Tests added

~20 new tests in `tests/test_investment_cash_integration.py`: operational
cash reduces/rejects/allows correctly (the exact 100m/60m/50m/40m
scenario); placement creates exactly one linked TreasuryTransaction;
termination/maturity increase operational cash correctly; full rollover
leaves operational cash unchanged; bank-balance-refresh double-count
protection; rebooking idempotency (same key vs. different key); monthly
periodic interest correct dates and no maturity duplication; upfront
interest at placement date; AT_MATURITY unchanged; multi-currency
isolation. One existing test renamed/reworked
(`test_placement_actually_reduces_operational_available_cash`) to assert
the real cash-position figure, not merely transaction existence.

### Full pytest result

Exact command:
```bash
cd backend && source .venv/bin/activate && python -m pytest -q
```
**171 collected, 171 passed, 0 failed, 0 skipped, exit code 0.** (158
prior + 13 new test functions — several tests added incrementally
during debugging were consolidated; the final file contains the
complete, correct set.) Actually executed, rerun after every code
change in this pass. Also re-verified the Stage 3 facility/funding-action
subset explicitly: 44/44 passed, confirming no regression from the
cash-position service change.

### Static checks

`ruff check` — all genuine issues auto-fixed (35 of 58); remaining
findings are exclusively the pre-existing `RUF059` stylistic pattern
already accepted throughout the codebase.
`mypy` on every file touched this pass (cash_position_service.py,
investment_engine.py, investment_service.py,
investment_forecast_adapter.py, api/v1/investments.py, schemas/investment.py,
models/investment.py) — **0 findings.**

### Frontend build

`✓ Compiled successfully`, `✓ Generating static pages (12/12)`, exit
code 0. No frontend files were changed this pass.

### Fresh DB migration

`CREATE DATABASE` → `alembic upgrade head`: all 14 migrations applied in
order, 49 tables, zero errors. Scratch database dropped after
verification.

### Alembic head count

Exactly one: `c68cb5de2aea`.

### Repository hygiene

`.gitignore` reviewed against this task's required minimum list —
already fully compliant via broader unrooted patterns (`.venv/` covers
`backend/.venv/`, `node_modules/` covers `frontend/node_modules/`,
`.next/` covers `frontend/.next/`, `*.py[cod]` covers `*.pyc`); no
changes needed. Secret scan clean (only placeholder/local-dev values in
`.env.example` and `README.md`).

### Known limitations

See `docs/STAGE_4_INVESTMENTS.md` section 27 for full detail: no
automatic `INTEREST_ACCRUAL` posting job; CUSTOM interest frequency has
no schedule generator (no stored dates to build from); frontend remains
read-only for write actions; no FX conversion for cross-currency
liquidity totals; operational available cash is a safe, correct
foundation but not full transaction-to-statement-line matching — that
remains explicitly Stage 5's job.

### Confirmation: Stage 0-3 remain intact

The full 171-test suite includes every Stage 0-3 test file, all passing
together. The Stage 3 facility/funding-action subset was additionally
re-run explicitly (44/44) specifically to confirm the cash-position
service change introduced no regression, since that service is shared
infrastructure.

### Confirmation

**"Stage 5 was NOT started."** No Bank Reconciliation, Intercompany
Reconciliation, AI Copilot, or external bank/payment-platform API code
was created or modified in this pass. The operational-cash mechanism is
explicitly documented as the minimum safe foundation Stage 5 will later
extend with real transaction-to-statement matching — not a
reimplementation of Stage 5 itself.
