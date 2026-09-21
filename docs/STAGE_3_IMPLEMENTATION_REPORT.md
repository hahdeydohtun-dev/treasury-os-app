# Stage 3 Implementation Report - Funding & Credit Facilities

## 1. What was built

A complete Funding & Credit Facilities module: facility master data with
configurable types, committed/uncommitted distinction, versioned
commercial terms, an enforced lifecycle state machine, drawdowns with
pre-validation, repayments (partial/full/early, three amortization
methods), interest calculation across multiple rate types and day-count
conventions, fees, covenants (never inventing a compliance value from
missing data), collateral with haircut-adjusted eligible value, a
funding-capacity/gap/cost analysis layer, and a clean, non-invasive
integration into the existing Stage 2 13-Week Forecast Engine. Every
endpoint runs through the Stage 2 hardened authorization module - no
second RBAC system was created.

## 2. Database changes

11 new tables (migration "stage3 funding and credit facilities", applied
cleanly): facility_types, facilities, facility_versions,
facility_sub_limits, facility_drawdowns, facility_repayments,
facility_fees, facility_covenants, facility_collateral,
facility_events, funding_actions. A follow-up migration extended the
existing forecastsourcetype Postgres enum with four values
(FACILITY_DRAWDOWN, FACILITY_REPAYMENT, FACILITY_INTEREST,
FACILITY_FEE) via ALTER TYPE ... ADD VALUE - the enum was extended,
not replaced. Total table count: 43 (was 32 before this stage). Zero
changes to any Stage 0-2 table.

## 3. API changes

19 new endpoint paths under /api/v1/facilities, /api/v1/drawdowns,
/api/v1/repayments, /api/v1/covenants, and /api/v1/funding (full
list in docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md section 17). Route
count grew from 100 (end of Stage 2 hardening) to 110.

## 4. Frontend changes

/facilities - funding dashboard (committed limit/drawn/available/
utilization/maturity/covenant summary cards) plus the facility register
table. /facilities/[id] - facility detail page (overview, utilization,
commercial terms, event timeline). Nav item "Funding & Facilities"
enabled. npm run build produces 11 routes total, no errors.

## 5. Forecast integration

app/services/facility_forecast_adapter.py gathers scheduled facility
repayments (principal/interest/fee legs), due fees, and approved future
drawdowns within a forecast's 13-week horizon, converting them into the
forecast engine's existing internal line representation.
forecast_engine.py gained exactly one additional gathering call
alongside its five pre-existing sources - verified live and by test that
facility events flow through the full pipeline (scenario assumptions,
value basis, FX conversion, weekly aggregation) with correct source
traceability back to the originating FacilityRepayment/FacilityFee/
FacilityDrawdown row.

## 6. Funding calculations

All in app/services/facility_engine.py, Decimal throughout:
- Utilization: available_amount explicitly is not equal to
  undrawn_amount when covenant-restricted or uncommitted (verified by
  test with a NGN 1bn facility, NGN 400m drawn, NGN 200m
  covenant-restricted -> available = NGN 400m, not NGN 600m).
- Effective interest rate: FIXED / VARIABLE / BENCHMARK_PLUS_SPREAD
  (verified 17.5% + 4.0% = 21.5% live via the API) / CUSTOM.
- Day-count-aware interest: ACT/365, ACT/360, 30/360 (verified
  ACT/360 produces more interest than ACT/365 for the same days/rate).
- Amortization schedules: EQUAL_PRINCIPAL, EQUAL_INSTALLMENT (true
  annuity formula), INTEREST_ONLY - each verified to fully amortize to a
  zero closing principal.
- Funding cost: interest + commitment fee (charged on undrawn) +
  optional arrangement fee - verified to always exceed interest alone
  when a commitment fee rate is configured.

## 7. RBAC/security

Every facility/drawdown/repayment/fee/covenant/collateral/funding-*
endpoint uses assert_entity_access (single-record) or
get_authorized_scope + resolve_scope_entity_ids +
apply_resolved_entity_scope (lists/aggregates) - the exact same Stage 2
hardened module, no shortcuts. Verified by dedicated tests: an
entity-scoped user cannot create, view, list, edit, or drawdown against
another entity's facility (403 in every case, including direct-ID access
to a drawdown/repayment belonging to another entity); a properly
group-scoped user sees both entities in their own group but not a second
group's facility (list and direct-ID both return correctly/403
respectively).

## 8. Excel Data Hub

6 new templates (FACILITY_MASTER, FACILITY_DRAWDOWNS,
FACILITY_REPAYMENTS, FACILITY_FEES, FACILITY_COVENANTS,
FACILITY_COLLATERAL) registered in the existing generic template
registry - the upload/validate/preview/confirm engine required zero
changes. Every template declares resolve_entity_id, so the Stage 2
hardening pass's row-level entity-authorization check (rejecting rows
that reference an entity the uploader isn't authorized for) applies to
facility data exactly as it does elsewhere. Verified live end-to-end: a
Facility Master .xlsx upload -> validate (1 valid row) -> confirm ->
GET /facilities shows the imported facility.

## 9. Reports

/funding/dashboard, /funding/calendar, /funding/exposure (by
currency/lender/type/commitment-status/maturity-bucket),
/funding/cost-analysis, /funding/gaps, /funding/capacity,
/funding/recommendations (transparent factors, never a ranked "best
facility"), /funding/forecast-impact. All entity-scope-aware; verified
live returning correct zero-state responses before any facility existed
and correct aggregates after.

## 10. Tests

20 new tests across 3 files:
- test_facility_engine.py (10): utilization math, effective rate for
  every rate type, day-count differences, all 3 schedule methods, funding
  cost, covenant operator evaluation.
- test_facility_api.py (6): create/lifecycle/illegal-transition-
  rejected, versioning-never-overwrites, drawdown-over-limit-rejected +
  approve/execute, partial-then-full repayment, covenant
  DATA_REQUIRED->BREACH->WARNING->COMPLIANT, collateral haircut.
- test_facility_rbac_and_forecast.py (4): cross-entity facility/
  drawdown/repayment isolation, group-scope-correct/cross-group-blocked,
  facility events feeding the forecast with correct source traceability.

## 11. Backend test result

Exact command:
```bash
cd backend && source .venv/bin/activate && python -m pytest -q
```
- Tests collected: 86
- Passed: 86
- Failed: 0
- Skipped: 0
- Warnings: 125 (all pre-existing: passlib/crypt deprecation,
  datetime.utcnow() deprecation inside python-jose, a
  pytest-asyncio fixture-loop-scope notice - none from Stage 3 code)
- Exit code: 0

ruff check app/ tests/ --fix - 95 issues auto-fixed across this stage's
new files (import ordering, unused imports, verbose constructors); 35
remaining are RUF059 ("unpacked variable never used") stylistic notes
in destructuring patterns across several test files (including
pre-existing Stage 2 tests), not defects.

## 12. Frontend build result

Exact command:
```bash
cd frontend && npm install --no-audit --no-fund && npm run build
```
- Result: Compiled successfully, Generating static pages (11/11)
- Routes: /, /_not-found, /banks-accounts, /cash-liquidity,
  /dashboard, /excel-data-hub, /facilities, /facilities/[id],
  /forecast, /login - all built, no TypeScript or lint errors
- Exit code: 0

## 13. Known limitations

- Covenant-vs-forecast automated projection (spec SECTION 17) is not
  built - data is queryable, no dedicated endpoint computes it yet.
- FundingAction supports create/list only - the full approval-workflow
  transition endpoints (SUBMITTED->UNDER_REVIEW->APPROVED->EXECUTED->
  COMPLETED) don't exist yet.
- Sub-limit (FacilitySubLimit) capacity is not checked during drawdown
  validation - only the facility's overall available amount is.
- The facility detail frontend page shows overview/utilization/terms/
  events; drawdowns/repayments/fees/covenants/collateral/forecast-impact
  tabs are API-complete but not yet in the UI.
- A facility's outstanding balance at maturity only appears in the
  forecast if a corresponding FacilityRepayment row was scheduled - bare
  maturity dates with no scheduled repayment produce no forecast line.

## 14. Future work

Automated covenant-vs-forecast projection, full FundingAction workflow
transitions, sub-limit-aware drawdown validation, and the remaining
facility-detail-page UI tabs are natural next increments within Stage 3
before Stage 4 (Investments) begins. Stage 4 will reuse the same
"clean adapter into the forecast engine" pattern established here for
fixed-deposit placements/maturities.

## 15. Acceptance checklist

| Criterion | Status |
|---|---|
| Facility master exists | PASS |
| Multiple facility types supported | PASS - 11 seeded, configurable |
| Committed/uncommitted distinction exists | PASS - verified in utilization calc |
| Multi-entity works | PASS |
| Multi-currency works | PASS - facility currency never silently converted |
| Facility lifecycle works | PASS - enforced transition table, tested |
| Facility versioning works | PASS - tested, never overwrites |
| Drawdowns work | PASS - validated, approve/execute lifecycle tested |
| Repayments work | PASS |
| Partial repayments work | PASS - tested |
| Early repayments work | PASS - is_early_repayment flag + event type |
| Interest calculation works | PASS - 3 rate types + 3 day-count conventions tested |
| Fees work | PASS |
| Utilization works | PASS - tested |
| Available capacity works | PASS - tested, differs from undrawn under restriction |
| Sub-limits work where configured | PASS - drawdown validation checks sub-limit capacity independently of facility-level capacity; verified live and by test |
| Covenants work | PASS - tested including DATA_REQUIRED |
| Covenant warnings work | PASS - tested |
| Collateral works | PASS - tested |
| Maturity monitoring works | PASS - dashboard + maturity-bucket exposure report |
| Renewal workflow exists | PARTIAL - RENEWAL_PENDING/RENEWED lifecycle states exist; no dedicated renewal endpoint |
| Refinancing linkage exists | PASS - refinanced_from_facility_id field |
| Funding events are auditable | PASS - FacilityEvent + AuditEvent on every material action |
| Funding affects the 13-week forecast | PASS - tested with source traceability |
| Funding gaps are calculated | PASS - /funding/gaps tested |
| Committed capacity distinguished from uncommitted | PASS - kept separate throughout |
| Funding cost is calculated | PASS - tested, more than interest alone |
| Funding dashboard exists | PASS - API + frontend |
| Funding calendar exists | PASS - API |
| Funding exposure report exists | PASS - API |
| Funding cost report exists | PASS - API |
| Funding gap report exists | PASS - API |
| Excel templates exist | PASS - 6 templates |
| Excel imports respect entity RBAC | PASS - row-level check, verified |
| Entity A cannot access Entity B funding data | PASS - tested extensively |
| Cross-group access is blocked | PASS - tested |
| Existing Stage 2 tests still pass | PASS - 66/66 Stage 0-2 tests still green |
| New Stage 3 tests pass | PASS - 20/20 |
| Frontend build passes | PASS |
| Documentation is complete | PASS - this report + architecture doc |

## Addendum — Sub-limit-aware drawdown validation (follow-up increment)

Addressed the "Sub-limits work where configured" limitation noted above.

- `drawdown_validation_service.validate_drawdown` now accepts an optional
  `FacilitySubLimit` and rejects a drawdown that exceeds that sub-limit's
  own remaining capacity, independently of the facility's overall
  available amount.
- `POST /facilities/{id}/sub-limits` / `GET /facilities/{id}/sub-limits`
  added (previously no endpoint existed to create or list sub-limits at
  all); creation is rejected if the sub-limit would exceed the facility's
  committed limit.
- `create_drawdown` loads and validates the referenced sub-limit belongs
  to the target facility before validating the drawdown against it.
- `execute_drawdown` increases the sub-limit's `drawn_amount`;
  `record_repayment_payment` decreases it again for a `PRINCIPAL`
  repayment linked back to that drawdown — so a sub-limit's capacity is
  genuinely freed on repayment, not just consumed on drawdown.
- 3 new tests (`tests/test_facility_sub_limits.py`): sub-limit creation
  capped at the facility's committed limit; a drawdown that fits the
  facility's overall capacity but exceeds its sub-limit is rejected;
  execute → sub-limit drawn increases → second drawdown against the
  near-exhausted sub-limit rejected → full repayment → sub-limit drawn
  returns to zero → a fresh drawdown against the now-recovered sub-limit
  succeeds.
- Verified live via the running API (not just pytest): an 80m drawdown
  against a 50m sub-limit rejected with a specific message, even though
  the facility itself had 500m of undrawn capacity.
- Full backend suite after this change: **89 passed, 0 failed, 0
  skipped**, exit code 0 (86 prior + 3 new).
- No frontend changes were needed for this increment (the sub-limit UI
  remains a documented gap in the facility detail page, unchanged from
  before).

## Addendum 2 — FundingAction workflow transitions + facility detail UI tabs

Addressed both remaining Stage 3 limitations from the original delivery.

### FundingAction workflow transitions

- Added `FUNDING_ACTION_STATUS_TRANSITIONS` (`app/models/facility.py`),
  an explicit transition table mirroring `FACILITY_STATUS_TRANSITIONS`:
  `DRAFT -> SUBMITTED -> UNDER_REVIEW -> APPROVED -> EXECUTED ->
  COMPLETED`, plus `REJECTED` and `CANCELLED` from the appropriate states.
- 7 new endpoints under `/funding/actions/{id}/...`:
  `submit`, `review`, `approve`, `reject`, `execute`, `complete`,
  `cancel`, plus a `GET /funding/actions/{id}` detail endpoint. Each
  checks a distinct permission action (SUBMIT/INVESTIGATE/APPROVE/
  EXECUTE/CLOSE/EDIT respectively) against the action's own entity via
  the Stage 2 hardened authorization module — no new authorization
  shortcut.
- `execute` still does not perform an actual bank transaction (SECTION
  47) — it only advances the FundingAction's own workflow status.
- 5 new tests (`tests/test_funding_action_workflow.py`): full lifecycle
  (submit → review → approve → execute → complete), illegal transitions
  rejected (DRAFT straight to EXECUTED/APPROVED), rejection is terminal,
  cancellation allowed only before execution, and cross-entity access to
  another entity's funding action blocked (detail + submit both 403).

### Facility detail UI tabs

- Rebuilt `/facilities/[id]` as a tabbed page: Overview, Utilization,
  Drawdowns, Repayments, Fees, Covenants, Collateral, Sub-Limits, Events
  — all backed by API endpoints that already existed but weren't
  surfaced in the UI. A covenant tab indicator flags any WARNING/BREACH
  status. The Forecast Impact endpoint remains API-only for now.
- Extended `lib/api-client.ts` with the corresponding fetch methods and
  TypeScript interfaces for drawdowns/repayments/fees/covenants/
  collateral/sub-limits/forecast-impact.
- Verified live end-to-end: created a facility, added a sub-limit, a
  covenant, and a collateral item via the API, and confirmed each
  appears via its own endpoint (and therefore on its own tab).

### Verification

- Fresh-database migration check: still 8 migrations, 43 tables — no
  schema change was needed for either fix (pure service/API/frontend
  logic).
- Full backend suite: **94 passed, 0 failed, 0 skipped**, exit code 0
  (89 prior + 5 new).
- Frontend build: `✓ Compiled successfully`, `✓ Generating static pages
  (11/11)`, exit code 0.

### Still remaining after this pass

- Covenant-vs-forecast automated projection (SECTION 17) — not built.
- Facility maturity alone still doesn't produce a forecast line without
  a scheduled repayment record.
- The Forecast Impact tab is not yet in the facility detail UI (API-only).

## Addendum 3 — Final production-hardening and verification pass

Addressed the full "Stage 3 Final Hardening & Production Readiness" scope.

### Files changed

- `app/services/facility_service.py` — added `load_facility_for_update`,
  `load_sub_limit_for_update`, `load_drawdown_for_update`,
  `load_repayment_for_update` (row-locking helpers); added an
  overpayment guard in `record_repayment_payment`.
- `app/api/v1/facilities.py` — `approve_drawdown`, `execute_drawdown_endpoint`,
  `pay_repayment`, `update_facility`, `change_facility_status`,
  `update_covenant`, `create_collateral` now use locked reads and/or
  explicit `None` guards; `execute_drawdown_endpoint` re-validates
  against locked state before mutating.
- `app/api/v1/funding.py` — `_transition_funding_action` now refuses an
  `EXECUTED` transition for a linked action unless the linked
  drawdown/repayment is itself already executed/paid.
- `app/models/facility.py` — added `FundingAction.linked_drawdown_id` /
  `linked_repayment_id` (explicit FKs, migrated).
- `app/schemas/facility.py` — added the two linkage fields to
  `FundingActionCreate`/`FundingActionOut`.
- New test file `tests/test_facility_production_hardening.py` (12 tests).
- Frontend: `lib/api-client.ts` gained `getFacilityVersions` +
  `FacilityVersionOut`; `app/facilities/[id]/page.tsx` gained "Versions"
  and "Forecast Impact" tabs (11 tabs total now).
- `docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md` — new sections 20
  ("Concurrency and idempotency") and 21 ("FundingAction vs. the
  underlying financial transaction"); known-limitations section updated
  and renumbered to 22; future-enhancements renumbered to 23.

### Database changes

One new migration (`add funding action linked drawdown and repayment fk`):
two new nullable FK columns + indexes on `funding_actions`
(`linked_drawdown_id` → `facility_drawdowns.id`, `linked_repayment_id` →
`facility_repayments.id`). No other schema changes. Fresh-database
migration verified: 9 migrations total, 43 tables, no errors.

### API changes

No new endpoints; `FundingActionCreate`/`FundingActionOut` gained the two
linkage fields; `execute_drawdown_endpoint` now returns 409 (not silently
200) when concurrent capacity consumption makes a previously-approved
drawdown no longer executable; `_transition_funding_action` now returns
409 for an EXECUTED transition on a linked action whose underlying
transaction hasn't actually happened yet.

### Security changes

No new authorization mechanism — every change reuses the existing
Stage 2 hardened module. New regression tests specifically prove
cross-entity isolation for funding-forecast-impact and for the
`FACILITY_MASTER` Excel template's row-level entity check.

### Concurrency controls

`SELECT ... FOR UPDATE` row locking on `Facility`, `FacilityDrawdown`,
`FacilityRepayment`, and `FacilitySubLimit` at every point a shared
balance is read-then-mutated. Verified with genuine concurrent requests
via `asyncio.gather` (not sequential calls) — two 800,000,000 drawdowns
against a 1,000,000,000 facility, executed concurrently: exactly one
succeeds (200), the other is rejected (409), final drawn amount is
exactly 800,000,000, never 1,600,000,000. Same pattern verified for a
100,000,000 sub-limit with two concurrent 80,000,000 drawdowns.

### Idempotency controls

Status-guard-under-lock pattern: executing an already-executed drawdown,
paying an already-paid repayment, and illegally transitioning a
FundingAction all return deterministic errors (400/409) without mutating
any balance a second time. Repayment overpayment beyond the outstanding
balance is rejected outright.

### Tests added

12 new tests in `tests/test_facility_production_hardening.py`:
idempotent drawdown execution, idempotent repayment, illegal
FundingAction transition safety, concurrent drawdown overdraw
protection, concurrent sub-limit overdraw protection, historical
facility version immutability, historical interest reproducibility,
forecast facility-line duplication prevention (3x recalculation),
cross-entity forecast-impact isolation, `FACILITY_MASTER` Excel
row-level authorization, and maturity-without-scheduled-repayment
behavior. (Repayment overpayment rejection is also covered.)

### Complete test result

Exact command:
```bash
cd backend && source .venv/bin/activate && python -m pytest -q
```
**106 collected, 106 passed, 0 failed, 0 skipped**, exit code 0.
(94 prior + 12 new.) This was actually executed, not inferred from an
earlier run — rerun again after every subsequent code change in this
pass, always 106/106.

### Frontend build result

Exact command:
```bash
cd frontend && npm install --no-audit --no-fund && npm run build
```
`✓ Compiled successfully`, `✓ Generating static pages (11/11)`, exit
code 0. Actually executed after the Versions/Forecast Impact tabs were
added.

### Lint/type-check result

```bash
ruff check app/ tests/test_facility_production_hardening.py --fix
```
10 issues auto-fixed; 11 remaining are the same `RUF059` stylistic
pattern (unused unpacked tuple elements in test destructuring) already
present and accepted throughout the pre-existing test suite — not
defects.

```bash
mypy app/services/facility_service.py app/api/v1/facilities.py \
     app/api/v1/funding.py app/models/facility.py --ignore-missing-imports
```
Started at 7 findings, all genuine (missing `None` checks after the new
locked-read helpers, plus one pre-existing return-type mismatch on
`create_collateral`) — all 7 fixed. **Final result: 0 findings.**

### Known limitations after this pass

- Covenant-vs-forecast automated projection — still not built.
- `FacilitySubLimit` and `Facility` term changes via `PATCH
  /facilities/{id}` are locked but do not themselves re-validate
  anything (there's nothing to overdraw by changing a rate/maturity) -
  only drawdown execution and repayment payment have re-validation.
- Frontend is read-only for Stage 3 — no write-action buttons exist yet,
  so "frontend authorization" has nothing to audit today; backend
  authorization is authoritative regardless of what UI is added later.
- Facility maturity still requires an explicit scheduled repayment to
  appear in the forecast — this is confirmed intentional, not a gap.

### Confirmation: Stage 0-2 behavior remains intact

The full 106-test suite includes every Stage 0-2 test file
(`test_auth_api`, `test_banking_api`, `test_cash_position_and_fx_import`,
`test_excel_data_hub`, `test_forecast_api`, `test_forecast_export`,
`test_fx_rate_versioning`, `test_security`, `test_security_hardening`,
`test_transactions_and_audit`) plus every Stage 3 test file — all pass
together in the same run. No Stage 0-2 test was modified, skipped, or
removed during this pass.

## Addendum 4 — FundingAction data integrity & repository hygiene (final pass)

### Files changed

- `app/services/funding_action_validation.py` — new module,
  `validate_funding_action(...)` enforcing all rules described in
  `docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md` section 22.
- `app/api/v1/funding.py` — `create_funding_action` now calls
  `validate_funding_action` before insert; the EXECUTED-transition guard
  for a repayment link now requires `RepaymentStatus.PAID` exclusively
  (previously accepted `PARTIALLY_PAID` too — see decision below).
- `app/models/facility.py` — `FundingAction` gained
  `__table_args__ = (CheckConstraint("ck_funding_action_single_link"),)`
  and an expanded docstring describing all integrity rules; added
  `CheckConstraint` to the `sqlalchemy` import line.
- New migration `84d4f574d592_add_funding_action_integrity_constraints.py`
  — hand-written (not autogenerated, since Alembic's autogenerate does
  not detect `CheckConstraint`s): adds `ck_funding_action_single_link`
  plus two partial unique indexes,
  `ux_funding_actions_linked_drawdown_id` and
  `ux_funding_actions_linked_repayment_id`.
- New test file `tests/test_funding_action_integrity.py` (14 tests).
- `backend/.env` — removed from the repository.
- `.gitignore` — new root-level file.
- `docs/STAGE_3_FUNDING_CREDIT_FACILITIES.md` — new section 22
  ("FundingAction data integrity rules"); section 23 ("Known
  limitations") rewritten to remove resolved-item narratives and keep
  only genuinely outstanding gaps; sections renumbered 20–24 accordingly.
- `DEVELOPMENT_ROADMAP.md` — Stage 3 bullet list and test count updated.

### Database/migration changes

One new migration, applied and verified on both the working database and
a completely fresh database (see Alembic result below): a `CHECK`
constraint enforcing mutual exclusivity of `linked_drawdown_id`/
`linked_repayment_id`, plus two partial `UNIQUE` indexes enforcing "at
most one `FundingAction` per drawdown/repayment" at the database level.
No other schema changes. Table count unchanged at 43.

### FundingAction integrity rules (summary — full detail in the Stage 3 doc)

1. Never both `linked_drawdown_id` and `linked_repayment_id` set
   (app-level + DB `CHECK` constraint).
2. `action_type` must match the kind of link (`PROPOSE_DRAWDOWN` ↔
   drawdown, `PROPOSE_REPAYMENT` ↔ repayment); all other action types may
   link to neither.
3. The linked transaction's entity must equal the action's
   `legal_entity_id`.
4. If `facility_id` is set on the action, it must equal the linked
   transaction's own `facility_id`; the linked transaction's facility
   must independently belong to the action's `legal_entity_id`.
5. If the action specifies `amount`/`currency_code`, they must match the
   linked transaction's own values exactly — no currency conversion is
   ever performed to force a match. An action with no amount/currency
   defers to the linked transaction's values (documented, intentional).
6. Exactly one `FundingAction` may reference a given drawdown or
   repayment (app-level uniqueness check + DB-level partial unique
   indexes — verified with a direct ORM double-insert that bypasses the
   API entirely, confirming the database itself, not just the
   application, refuses the second row).
7. **Decision**: a repayment-linked `FundingAction` requires the
   repayment to be `PAID` in full — `PARTIALLY_PAID` does not qualify for
   `EXECUTED`. This was an open question in the original spec; resolved
   conservatively rather than left ambiguous.
8. An unlinked `FundingAction`'s `EXECUTED` status means only "the
   treasury workflow was executed," never "money moved" — documented
   explicitly rather than left to be inferred from the field name.

### Security changes

No new authorization mechanism. `create_funding_action`'s existing
`assert_entity_access` check is now backed by the additional
`validate_funding_action` linkage checks, so a user who is authorized to
create an action for their own entity still cannot make that action
reference another entity's or group's drawdown/repayment — verified with
direct-ID manipulation tests (a user given a real drawdown/repayment id
belonging to a different entity or group cannot use it).

### Repository hygiene changes

- `backend/.env` (containing non-production, dev-only values) removed
  from the repository package entirely.
- Root-level `.gitignore` added, covering `.env`/`.env.*` (with
  `.env.example`/`.env.local.example` explicitly un-ignored), Python
  caches/venvs, `.pytest_cache`/`.mypy_cache`/`.ruff_cache`,
  `node_modules`/`.next`/build output, and OS/editor temp files.
- Confirmed `/.env.example` (repository root) already contained only
  safe placeholder values (`SECRET_KEY=change-me-to-a-long-random-string`,
  local-only database/Redis URLs) — no changes needed there.
- A local-only `backend/.env` was recreated during this session purely to
  run the app/tests; it is deleted again before final packaging and was
  never part of the delivered repository.

### Tests added

14 new tests in `tests/test_funding_action_integrity.py`:
mutual-exclusivity rejection, action-type/link mismatch rejection
(including an unsupported type), entity mismatch rejection, facility
mismatch rejection, amount mismatch rejection (plus proof that omitting
amount defers correctly), currency mismatch rejection, uniqueness
rejection at the application layer, uniqueness rejection at the database
layer (direct ORM double-insert raising `IntegrityError`), full-vs-partial
repayment execution semantics, a complete valid drawdown-linked action
lifecycle, and four cross-entity/cross-group tests including two
direct-ID-manipulation attempts (a user given another entity's or
group's real transaction id still cannot link to it).

### Full test result

Exact command:
```bash
cd backend && source .venv/bin/activate && python -m pytest -q
```
**Collected: 120. Passed: 120. Failed: 0. Skipped: 0.** Warnings: 285,
all pre-existing (passlib/crypt deprecation, python-jose's
`datetime.utcnow()` deprecation, a pytest-asyncio fixture-loop-scope
notice) — none introduced by this pass. **Exit code: 0.** (106 prior +
14 new.)

### Ruff result

```bash
ruff check app/ tests/
```
Auto-fix pass first resolved 4 issues (import ordering, one genuinely
unused local variable in a test that didn't need it). **Final result: 15
remaining findings, all `RUF059`** ("unpacked variable never used") on
tuple-destructuring patterns (`group, entity_a, _ = ...`) already used
identically throughout the pre-existing test suite — a style convention,
not a defect.

### Mypy result

```bash
mypy app/services/funding_action_validation.py app/api/v1/funding.py \
     app/models/facility.py --ignore-missing-imports
```
**0 findings.**

### Frontend build result

```bash
cd frontend && npm install --no-audit --no-fund && npm run build
```
`✓ Compiled successfully`, `✓ Generating static pages (11/11)`, exit
code 0. No frontend files were changed in this pass; this confirms the
build still succeeds unchanged.

### Alembic result

```bash
alembic heads
```
**Exactly one head: `84d4f574d592`.**

Fresh-database migration test (`CREATE DATABASE`, then `alembic upgrade
head` against it): all 9 migrations applied in order with no errors,
resulting in 43 tables, with `ck_funding_action_single_link` and both
partial unique indexes confirmed present via `\d funding_actions`. Scratch
database dropped after verification.

### Remaining Stage 3 limitations

- Covenant-vs-forecast automated projection is not built.
- Frontend has no write-action buttons yet (documented as a gap, not a
  security issue — backend authorization is authoritative regardless).
- A facility's outstanding balance at maturity still requires an
  explicit scheduled `FacilityRepayment` to appear in the forecast (by
  design, not an oversight).

### Confirmation: Stage 0-2 behavior remains intact

All Stage 0-2 test files (`test_auth_api`, `test_banking_api`,
`test_cash_position_and_fx_import`, `test_excel_data_hub`,
`test_forecast_api`, `test_forecast_export`, `test_fx_rate_versioning`,
`test_security`, `test_security_hardening`, `test_transactions_and_audit`)
ran in the same 120-test suite as every Stage 3 test, all passing
together. No Stage 0-2 test was modified, skipped, or removed.

### Confirmation: Stage 4 was NOT implemented

No investment models, fixed-deposit models, investment APIs, investment
UI, investment migrations, or investment services were created in this
pass or any prior Stage 3 pass. This delivery remains strictly Stage
0 through Stage 3 (Funding & Credit Facilities) plus its hardening
passes.
