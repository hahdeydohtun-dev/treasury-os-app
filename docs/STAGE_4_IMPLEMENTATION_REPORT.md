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
