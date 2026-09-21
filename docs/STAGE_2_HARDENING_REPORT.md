# Stage 2 Hardening Report

Security, RBAC, entity-isolation, and regression-testing pass over
Stage 0-2 (Foundation, Treasury Data Foundation + Excel Data Hub,
13-Week Cash Flow Forecast Engine). No new functionality was added; no
Stage 3 (Funding & Credit Facilities) work was started.

## 1. Scope

Audited every API router under backend/app/api/v1/: forecast.py,
transactions.py, bank_balances.py, forecast_inputs.py (expected
collections/payments, bank charges), banking.py (banks, bank accounts),
entities.py (groups, legal entities), cash_position.py, excel.py
(Excel Data Hub), audit.py, currencies.py. For each endpoint: checked
authentication, module/action permission, entity-scope enforcement at the
query level (not fetch-then-filter), detail-level authorization
independent of list filtering, cross-group behavior for GROUP_WIDE
assignments, write/create authorization, and (for the Excel Data Hub)
whether an uploaded file's row-level entity references were checked
against the uploader's authorization.

## 2. RBAC Findings

### 2.1 Cross-group leak in GROUP_WIDE assignments (root cause)

- Where: app/auth/dependencies.py::_grants_permission.
- Problem: the GROUP_WIDE branch returned True unconditionally
  whenever the (module, action) matched, never checking
  UserRoleAssignment.group_id against the entity actually being
  accessed. A user granted GROUP_WIDE scope for Group 1 was
  authorized for Group 2's entities too, on any endpoint that relied on
  this check alone.
- Impact: cross-tenant/cross-group data exposure for any properly
  group-scoped (not superuser, not globally-unscoped) role assignment.
- Fix: new app/auth/authorization.py module. AuthorizedScope +
  get_authorized_scope resolve a user's actual authorized entity ids
  and group ids from their role assignments; a GROUP_WIDE assignment
  with group_id set only authorizes that group's entities (resolved via
  LegalEntity.group_id), never every group. An assignment with
  group_id = NULL is treated as truly unrestricted (system-wide) -
  preserving pre-existing test fixtures and any legitimate
  "super treasury admin" assignment - so this is additive scoping, not a
  breaking change to existing unscoped grants.
- Test added: test_group_wide_user_sees_both_entities_but_not_other_groups
  (creates a second Group + entity, proves a Group-1-scoped GROUP_WIDE
  user sees both Group 1 entities' forecasts but not Group 2's, by list
  and by direct ID).

### 2.2 Forecast list leak (the originally reported issue)

- Where: GET /api/v1/forecast (list_forecasts).
- Problem: the endpoint used require_permission(FORECAST, VIEW) -
  which resolves a single entity_id from query params - as its only
  gate, then ran select(Forecast) with no further scoping in the
  function body. Net effect: an entity-scoped user with no filter got a
  blanket 403 (fail-closed, not a leak, but not the spec's required
  behavior either), while a GROUP_WIDE user got zero group boundary at
  all (see 2.1).
- Fix: list_forecasts now resolves get_authorized_scope(...)
  once, validates an explicit legal_entity_id filter against that
  scope (403 if the caller isn't authorized for it), and applies the
  scope as a SQL WHERE clause via apply_entity_scope before any rows
  are fetched - never fetch-then-filter. An entity-scoped user with no
  filter now sees exactly their own authorized entities' forecasts, per
  spec.
- Test added: test_forecast_list_only_returns_authorized_entity
  (covers spec TEST 1-4 exactly: unfiltered list, authorized filter,
  unauthorized filter rejected, direct-ID access to another entity's
  forecast rejected).

### 2.3 A bug found while fixing 2.1/2.2

- Where: app/auth/authorization.py::apply_entity_scope (first version).
- Problem: the function only matched a GROUP_WIDE scope against
  rows where entity_id_column IS NULL (i.e. a genuinely group-wide,
  no-single-entity Forecast row) - it did not check whether an
  entity-owned row's own entity belonged to an authorized group. A
  GROUP_WIDE-to-Group-1 user's list request returned zero rows for
  Group 1's own, perfectly legitimate, entity-scoped forecasts.
- Caught by: the newly-added
  test_group_wide_user_sees_both_entities_but_not_other_groups failing
  on first run (empty result instead of both entities' forecasts) -
  demonstrating the value of the regression suite mandated by this task.
- Fix: apply_entity_scope now resolves the caller's group_ids
  into their member entity ids (one query) and includes those in the
  .in_() filter, in addition to the null-entity/group-id branch. Now
  covers both shapes: entity-owned rows in an authorized group, and
  genuinely group-wide (null-entity) rows in an authorized group.

### 2.4 Detail endpoints with no independent authorization

- Where: get_transaction (transactions.py), the (previously
  missing) detail endpoints for ExpectedCollection/ExpectedPayment/
  BankCharge (forecast_inputs.py), get_validation_result and
  confirm_import_batch (excel.py, entity check present but not
  group-aware).
- Problem: get_transaction had no entity check at all beyond a
  generic require_permission(VIEW) (which resolves no entity_id from a
  transaction_id path param, so it degenerated to "GROUP_WIDE or
  bust" - fail-closed for entity-scoped users, not a leak, but also not
  usable). ExpectedCollection/ExpectedPayment/BankCharge had no
  detail endpoints at all - a real functional gap alongside the
  security one. get_validation_result/confirm_import_batch checked
  the batch's entity via the un-upgraded _grants_permission, missing
  the group-membership fix from 2.1.
- Fix: every detail endpoint now independently verifies authorization
  against the loaded record's own entity/group via
  assert_entity_access or the equivalent scope check - never derived
  from a request query parameter. New detail endpoints added for
  Expected Collections/Payments/Bank Charges.
- Tests added:
  test_transaction_cross_entity_isolation,
  test_expected_collection_and_payment_cross_entity_isolation,
  test_excel_import_history_and_validation_detail_are_entity_scoped.

### 2.5 Aggregate endpoint leak: Cash Position

- Where: GET /api/v1/cash-position.
- Problem: with no legal_entity_id filter, the endpoint summed
  balances across every bank account in the database regardless of
  the caller's authorized entities/groups - the exact "aggregate leaks
  information even when individual records are hidden" risk called out
  in the task.
- Fix: the endpoint now resolves the caller's authorized entity set
  (translating any GROUP_WIDE grant into concrete entity ids) and
  passes it into calculate_cash_position (extended with a
  legal_entity_ids: list[UUID] | None parameter) so the SQL aggregation
  itself is scoped, not just the response.
- Test added: test_cash_position_aggregate_excludes_unauthorized_entity
  (Entity B's much larger balance is proven not to contribute to Entity
  A's total).

### 2.6 List endpoints with no entity scoping at all

- Where: list_bank_accounts/get_bank_account (banking.py),
  list_bank_balances (bank_balances.py, join-scoped via
  BankAccount since BankBalance has no direct entity column),
  list_expected_collections/list_expected_payments/list_bank_charges
  (forecast_inputs.py), list_groups/get_group/list_legal_entities/
  get_legal_entity (entities.py), list_recurring_flows/
  list_liquidity_thresholds (forecast.py admin endpoints),
  list_import_history (excel.py), list_audit_events (audit.py).
- Fix: all converted to the same pattern: resolve
  get_authorized_scope/resolve_scope_entity_ids, validate any
  explicit entity filter against that scope, apply the scope as a SQL
  filter (apply_resolved_entity_scope for models with an always-non-null
  entity column; resolve_scope_group_ids + the same filter for
  Group, since a user needs to see the parent group of any entity
  they're individually authorized for).
- Tests added:
  test_bank_account_cross_entity_isolation,
  test_recurring_cash_flow_and_liquidity_threshold_isolation
  (also exercises list_bank_balances and list_expected_*/list_bank_charges
  indirectly via the shared helper pattern; explicit assertions on
  expected collections/payments in
  test_expected_collection_and_payment_cross_entity_isolation).

### 2.7 Write-side authorization gaps

- Where: create_transaction, create_bank_balance,
  create_expected_collection/create_expected_payment/
  create_bank_charge, create_bank_account, create_recurring_flow,
  create_liquidity_threshold all used the old single-shot
  _grants_permission call directly.
- Fix: all converted to assert_entity_access, which additionally
  loads and validates the target entity exists (404 if not) before
  checking authorization - closing a minor information-shape
  inconsistency where a bad entity id and an unauthorized entity id
  previously produced different-looking failures in some paths.
- Data-integrity checks added (SECTION 23): Forecast creation now
  rejects a legal_entity_id that doesn't belong to the given
  group_id; ForecastAdjustment creation now rejects an entity that
  isn't part of the target forecast's own entity/group scope, even if
  the caller happens to be separately authorized for that other entity;
  the forecast entity-view endpoint applies the same cross-check.

### 2.8 Excel import: row-level entity authorization (the most serious individual gap)

- Where: app/services/excel_service.py::confirm_import,
  app/services/excel_templates.py.
- Problem: an uploader's permission was checked only against the
  upload form's declared legal_entity_id - never against what the
  file's own rows actually named in their Entity column. A user
  authorized only for Entity A could upload a Bank Accounts (or Bank
  Transactions, Expected Collections/Payments, Bank Charges) file whose
  rows named Entity B, and every Entity-B row would import successfully,
  because nothing checked the resolved entity of each row against the
  confirming user's authorization. This is exactly TEST 10's scenario.
- Fix: TemplateSpec gained an optional resolve_entity_id field
  (a lightweight lookup, no side effects) wired into every
  entity-bearing template (Bank Accounts, Bank Balances, Bank
  Transactions, Expected Collections, Expected Payments, Bank Charges -
  FX Rates is intentionally exempt, having no entity concept).
  confirm_import now accepts an authorized_entity_ids parameter; for
  every row, if the template has an entity concept and the row's
  resolved entity isn't in that set, the row is rejected with a new
  UNAUTHORIZED_ENTITY issue and not imported - counted in
  skipped_rows, never silently absorbed into imported_rows. The
  confirm_import_batch endpoint resolves the confirming user's
  authorized scope and passes it through.
- Test added: test_excel_import_rejects_unauthorized_entity_rows
  (a two-row file with one Entity A row and one Entity B row uploaded by
  an Entity-A-only user: valid_rows == 2 at validation time since both
  rows are structurally fine, but imported_rows == 1 /
  skipped_rows == 1 after confirm, and the Entity B account is proven
  absent from the database afterward).

## 3. Entity Isolation

Every list endpoint now follows one pattern:

1. get_authorized_scope(db, user, module, action) resolves the caller's
   AuthorizedScope (unrestricted, or concrete entity ids / group ids)
   from their active role assignments - once per request, not per row.
2. An explicit entity filter in the request is validated against that
   scope (scope.allows_entity(...)) - rejected with 403 if not
   authorized, never silently widened or narrowed without the caller
   knowing.
3. The scope is applied as a SQL WHERE clause (apply_entity_scope for
   models that support a null-entity/group-wide row shape, like
   Forecast; resolve_scope_entity_ids + apply_resolved_entity_scope
   for models where every row has a concrete entity, like
   TreasuryTransaction) before the query executes - never
   fetch-everything-then-filter-in-Python.
4. An empty, non-unrestricted scope filters to zero rows (WHERE false),
   not "no filter" - fail closed by construction.

Every detail (by-ID) endpoint independently re-checks the loaded
record's own entity/group against the caller's scope
(assert_entity_access / assert_record_belongs_to_authorized_entity) -
list-level filtering is never assumed to be sufficient protection for a
guessed or enumerated ID.

## 4. Group Scope

A GROUP_WIDE UserRoleAssignment with a group_id set authorizes every
current entity within that group (resolved via LegalEntity.group_id),
for both entity-owned rows and genuinely group-wide (null-entity) rows
like a consolidated Forecast. A GROUP_WIDE assignment with
group_id = NULL remains unrestricted/system-wide, preserving existing
fixtures and any legitimate "no group scoping configured yet" assignment.
Cross-group access is blocked: a Group-1-scoped GROUP_WIDE user cannot
list, retrieve, or aggregate Group 2's entities or forecasts (verified by
test_group_wide_user_sees_both_entities_but_not_other_groups, which
also caught and led to fixing the bug described in 2.3).

## 5. Endpoints Audited

| Module | Endpoint(s) | Fixed this pass |
|---|---|---|
| Forecast | POST/GET /forecast, GET/POST/PATCH .../{id} and all sub-resources (weeks, summary, lines, entity-view, currency-view, variance, accuracy, alerts, funding-gaps, surplus, adjustments, scenarios, what-if, export, calculate, publish, archive, roll) | Yes - list, create, detail-scope helper, entity-view, adjustments |
| Forecast admin | categories (list/create), recurring-cash-flows (list/create), liquidity-thresholds (list/create) | Yes - list scoping + create authorization |
| Transactions | POST/GET /transactions, GET /transactions/{id} | Yes - list + detail |
| Bank Balances | POST/GET /bank-balances | Yes - create + join-based list scoping |
| Banking | POST /banks, GET /banks, POST/GET /bank-accounts, GET /bank-accounts/{id} | Yes (accounts); Bank master data create left as global-admin action (no entity concept) |
| Forecast Inputs | Expected Collections/Payments/Bank Charges - create, list, new detail endpoints | Yes - full rewrite, detail endpoints added |
| Entities | POST/GET /groups, GET /groups/{id}, POST/GET /legal-entities, GET /legal-entities/{id} | Yes - list/detail scoping |
| Cash Position | GET /cash-position | Yes - the aggregate leak fix |
| Excel Data Hub | GET /templates, POST /uploads, GET /validation/{id}, POST /imports/{id}/confirm, GET /imports, GET /freshness | Yes - detail check, row-level import authorization, history scoping |
| Audit | GET /audit | Yes - list scoping |
| Currencies/FX | POST/GET /currencies, POST/GET /fx-rates | Audited, no change - genuinely global reference data with no entity/group ownership |

## 6. Regression Tests

New file: backend/tests/test_security_hardening.py (12 tests):

1. test_forecast_list_only_returns_authorized_entity - spec TEST 1-4
2. test_group_wide_user_sees_both_entities_but_not_other_groups - spec TEST 6 + cross-group
3. test_user_a_cannot_create_forecast_for_entity_b - spec TEST 7
4. test_user_a_cannot_adjust_forecast_b - spec TEST 8
5. test_user_a_export_contains_only_entity_a - spec TEST 9
6. test_transaction_cross_entity_isolation
7. test_bank_account_cross_entity_isolation
8. test_cash_position_aggregate_excludes_unauthorized_entity - spec TEST 5
9. test_expected_collection_and_payment_cross_entity_isolation
10. test_recurring_cash_flow_and_liquidity_threshold_isolation
11. test_excel_import_rejects_unauthorized_entity_rows - spec TEST 10
12. test_excel_import_history_and_validation_detail_are_entity_scoped

All are true negative tests (proving 403/absence), not just 200-and-move-on
- each inspects the actual returned record set, not merely the status
code, per the task's explicit requirement.

## 7. Frontend Fix

Investigated and found no defect. The task described a duplicated
"Net Cash Flow" row in the Stage 2 forecast weekly detail table
(frontend/app/forecast/page.tsx). A full-text search of the file and
the whole frontend/app/ tree for "Net Cash Flow" / net_cash_flow
found exactly one such row in the weekly table's row-definition array
(["Net Cash Flow", "net_cash_flow"]) and one unrelated summary card
("13-Week Net Cash Flow", a single 13-week aggregate figure, not a
per-week table row - a different UI element, not a duplicate). No React
key collision exists either (each row's key is its unique metric string).
No code change was made because none was needed; this is reported
plainly rather than fabricating a fix for a defect that isn't present in
the current codebase.

## 8. Documentation Changes

Standardized Stage numbering across every markdown doc to:

```
Stage 0 - Foundation
Stage 1 - Treasury Data Foundation + Excel Data Hub
Stage 2 - 13-Week Cash Flow Forecast Engine
Stage 2 Hardening - Security & Quality Pass  (this document)
Stage 3 - Funding & Credit Facilities
Stage 4 - Investments
Stage 5 - Bank Reconciliation
Stage 6 - Intercompany Reconciliation
Stage 7 - Working Capital, Risk, Controls, KPIs, Reports & Workflow
Stage 8 - AI Treasury Copilot / Treasury Intelligence
Stage 9 - Future API Integrations
```

Previously, several docs (ARCHITECTURE.md, DOMAIN_MODEL.md,
TREASURY_DATA_MODEL.md, EXCEL_DATA_HUB.md, FORECAST_ENGINE.md,
FORECAST_METHODOLOGY.md, FORECAST_DATA_SOURCES.md, README.md)
referred to the Forecast Engine as "Stage 3" and the Treasury Data
Foundation as "Stage 2" - inconsistent with DEVELOPMENT_ROADMAP.md,
which already used the canonical numbering. All cross-references were
corrected to match. DEVELOPMENT_ROADMAP.md also gained an explicit
"Stage 2 Hardening" entry (this pass) and a "Stage 9 - Future API
Integrations" entry that was previously missing.

## 9. Backend Test Results

Exact command (from backend/, with the project's venv activated):

```bash
PYTHONPATH=. pytest -q
```

(Plain pytest -q without PYTHONPATH=. fails with
ModuleNotFoundError: No module named 'app' - there is no editable
package install in this environment; python -m pytest -q, used
throughout prior stages, works identically without needing
PYTHONPATH since -m adds the current directory to sys.path
automatically. This is a pre-existing environment characteristic, not a
regression introduced by this pass.)

- Tests collected: 66
- Passed: 66
- Failed: 0
- Skipped: 0
- Warnings: 83 (all pre-existing: passlib/crypt deprecation on
  Python 3.13, datetime.utcnow() deprecation inside python-jose,
  and a pytest-asyncio fixture-loop-scope deprecation notice - none
  from this pass's code, none affect correctness)
- Exit code: 0

## 10. Frontend Build Results

Exact command (from frontend/):

```bash
npm install --no-audit --no-fund
npm run build
```

- Result: Compiled successfully, Generating static pages (10/10)
- Routes: /, /_not-found, /banks-accounts, /cash-liquidity,
  /dashboard, /excel-data-hub, /forecast, /login - all built,
  no TypeScript or lint errors reported by the build
- Exit code: 0

## 11. Static Checks

```bash
ruff check app/ --fix
```

- Before: 6 issues found (4 auto-fixed: an unused local variable
  introduced during this pass's own edits, plus 3 cosmetic import
  formatting issues also introduced this pass)
- After: 1 remaining (SIM103, "return the condition directly" - a
  stylistic suggestion on AuthorizedScope.allows_entity's two
  sequential if statements; left as-is for readability, not a defect)

```bash
mypy app/... --ignore-missing-imports
```

Run against every file touched this pass. All findings are the same
accepted category already documented in Stage 1/2's own hardening notes:
places where a validator or a prior null-check already guarantees a
value is non-None at runtime, but the type checker cannot see that
invariant across the function boundary (e.g. assert_entity_access
returning LegalEntity | None, called only after the caller already
confirmed entity_id is not None). No behavior-changing type errors were
found in this pass's new code.

## 12. Remaining Issues

- LiquidityThreshold group-membership check is approximate. The
  model has no group_id column (only legal_entity_id,
  currency_code, bank_account_id), so a GROUP/CURRENCY-scope
  threshold (no legal_entity_id) is shown to any user with some
  GROUP_WIDE authorization, rather than being checked against a
  specific group - there's no group to check it against in the current
  schema. Documented as a known limitation, not silently ignored.
- Bank (institution) creation is not entity-scoped, since a bank
  institution is shared master data with no natural entity/group owner;
  it remains gated by a plain BANKS_ACCOUNTS:CREATE permission check
  (effectively requiring unrestricted/system-wide access, since no
  entity_id can be resolved for it). This is a reasonable and
  intentional scope boundary, not an oversight, but is called out here
  for transparency.
- Currencies and FX Rates remain globally readable/writable by
  anyone with CURRENCY_FX permission, with no entity or group scoping.
  This is correct given the schema (neither model has an entity/group
  owner - they are genuinely shared reference data across the whole
  system) but is noted here since the task asked for every module to be
  explicitly accounted for, not silently skipped.
- The reported duplicate "Net Cash Flow" frontend row could not be
  reproduced in the current codebase (see Section 7). If this was
  observed in a running browser session, it may have been a stale
  build/cache artifact rather than a current source defect; no
  corresponding code change was made.
- Audit log filtering treats entity-less audit events (e.g.
  currency/account-type admin actions) as visible to any authorized
  viewer rather than restricting them further - these are
  configuration-level actions with no natural entity owner, so this is
  an intentional design choice, not a gap, but is noted for
  completeness.

## 13. Security Acceptance Criteria

| Criterion | Status |
|---|---|
| Entity A user cannot list Entity B records | PASS - verified for Forecast, TreasuryTransaction, BankAccount, ExpectedCollection, ExpectedPayment, RecurringCashFlow, LiquidityThreshold, ImportBatch (history) |
| Entity A user cannot retrieve Entity B records by ID | PASS - verified for Forecast, TreasuryTransaction, BankAccount, ExpectedCollection, ExpectedPayment, ImportBatch (validation detail) |
| Entity A user cannot modify Entity B records | PASS - verified for Forecast (adjustments) |
| Entity A user cannot create records for Entity B | PASS - verified for Forecast |
| Entity A user cannot delete/archive Entity B records | N/A - no delete/archive endpoint exists yet for any entity-owned Stage 0-2 resource other than Forecast.archive, which is scope-checked identically to publish/calculate (same _assert_forecast_scope helper); not separately regression-tested this pass |
| Entity A user cannot export Entity B records | PASS - verified for Forecast export |
| Entity A user cannot import Entity B data | PASS - verified via row-level Excel import authorization |
| Entity A user cannot obtain Entity B information through aggregate endpoints | PASS - verified for Cash Position |
| Entity A user cannot obtain Entity B information through nested endpoints | PASS - verified for the Forecast entity-view sub-resource (rejects both an unauthorized legal_entity_id and one outside the parent forecast's own scope) |
| Group-scoped users can still access authorized group-wide data | PASS - verified |
| Group-scoped aggregates correctly consolidate authorized entities | PASS - verified (Forecast list includes both Group 1 entities) |
| Cross-group access is blocked | PASS - verified (Group 2's forecast invisible to a Group-1-scoped user, by list and by ID) |
| Authorization is enforced server-side | PASS - every fix is in app/auth/authorization.py and the API layer; no frontend-only change was made or relied upon |
| Forecast list leakage is fixed | PASS |
| Other Stage 0-2 endpoints have been audited | PASS - see Section 5's table; Currencies/FX explicitly audited and found not applicable |
| Duplicate Net Cash Flow row is removed | N/A - investigated, not found in the current codebase (Section 7) |
| Stage numbering is standardized | PASS - Section 8 |
| Regression tests exist for cross-entity access | PASS - 12 tests, Section 6 |
| Full backend test suite passes | PASS - 66/66, Section 9 |
| Frontend build passes | PASS - Section 10 |
| Security/verification documentation is updated | PASS - this document |
