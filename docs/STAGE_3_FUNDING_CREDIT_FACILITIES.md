# Stage 3 - Funding & Credit Facilities

## 1. Purpose

Gives Treasury a complete inventory of funding facilities - committed and
uncommitted, across lenders, currencies, and entities - and connects that
inventory directly to the 13-Week Cash Flow Forecast Engine (Stage 2), so
scheduled repayments, interest, fees, and approved drawdowns become real
forecast drivers rather than a disconnected register. The module answers,
with real backend calculations (never invented, never frontend-only):
available committed capacity, funding gaps, funding cost, covenant status,
and upcoming maturities.

## 2. Architecture

```
BANKS (existing, Stage 1)
   |
FACILITIES  (this stage: master + versioned terms)
   |
DRAWDOWNS / REPAYMENTS / FEES  (this stage: treasury-internal records,
   |                             never an actual bank execution - SECTION 47)
FUNDING CAPACITY  (committed available vs uncommitted potential, kept
   |                separate - never combined into one number)
13-WEEK FORECAST  (Stage 2, extended via a clean adapter - not rewritten)
   |
LIQUIDITY GAP  (funding_capacity_service: gap = shortfall - committed
   |            capacity applied; analytical only, never auto-executed)
TREASURY ATTENTION  (forecast alerts already surface gaps; facility
   |                 maturity/covenant signals are exposed via the funding
   |                 dashboard/calendar for the same integration point)
HUMAN DECISION -> APPROVAL -> TREASURY ACTION  (FundingAction workflow)
```

No new authorization model was introduced. Every Stage 3 endpoint uses the
same app/auth/authorization.py module built during the Stage 2 hardening
pass (get_authorized_scope, assert_entity_access,
resolve_scope_entity_ids, apply_resolved_entity_scope).

## 3. Facility types

FacilityType is a configurable lookup table (like Currency,
AccountType, CashEventType elsewhere in the system) - not a fixed
enum. Seeded with the 11 types from the spec
(app/db/seed_facility_types.py); new types are added as rows, never a
code change.

## 4. Lifecycle

FacilityStatus: DRAFT -> ACTIVE -> {SUSPENDED, EXPIRED, MATURED,
RENEWAL_PENDING, CLOSED}, plus RENEWED -> ACTIVE and terminal
CANCELLED. Allowed transitions are an explicit table
(FACILITY_STATUS_TRANSITIONS in app/models/facility.py) - an
out-of-table transition is rejected with a 400 explaining exactly which
transitions are currently allowed, never a silent no-op or an arbitrary
jump (e.g. DRAFT -> CLOSED directly is rejected). Every transition is
recorded as a FacilityEvent and an AuditEvent.

## 5. Data model

| Table | Role |
|---|---|
| facility_types | Configurable facility type lookup |
| facilities | Master record - current commercial terms, status, entity/lender/currency |
| facility_versions | Effective-dated snapshot of terms, one row per material change - never overwritten |
| facility_sub_limits | Optional purpose-restricted sub-limits within a facility's total |
| facility_drawdowns | Drawdown requests, with full approval lifecycle |
| facility_repayments | Scheduled/actual principal, interest, and fee repayments |
| facility_fees | Arrangement/commitment/processing/renewal/early-repayment/other fees |
| facility_covenants | Configurable covenant definitions + measured status |
| facility_collateral | Collateral/security items with haircut-adjusted eligible value |
| facility_events | Append-only, business-readable lifecycle timeline |
| funding_actions | Proposed funding actions (drawdown/repayment/refinance/renewal/limit-change) - a workflow record, never an automatic execution |

Every monetary field is Numeric (Python Decimal), never float
(SECTION 4/45). Every facility belongs to exactly one LegalEntity.

### Available != Undrawn

app/services/facility_engine.py::calculate_utilization computes:

```
undrawn_amount = committed_limit - drawn_amount
available_amount =
    0                                     if UNCOMMITTED
    max(0, undrawn - covenant_restricted) if COMMITTED
```

An UNCOMMITTED facility's available_amount is always 0 - uncommitted
capacity is never treated as guaranteed liquidity (SECTION 3). A
COMMITTED facility's available_amount also subtracts
covenant_restricted_amount (a stored field, set administratively when a
covenant or borrowing-base condition makes part of the undrawn limit
unavailable) - so available and undrawn are genuinely different
numbers whenever restrictions apply, exactly as the spec's worked example
requires.

## 6. Drawdowns

app/services/drawdown_validation_service.py::validate_drawdown checks,
before any drawdown is created: facility is ACTIVE; requested currency
matches the facility's currency; drawdown date is within the
availability window and before maturity; requested amount does not
exceed available_amount (or undrawn_amount for an uncommitted
facility, since availability is never assumed there either). Every
rejection returns a specific, human-readable reason - never a bare 400.

Lifecycle: SUBMITTED -> APPROVED -> EXECUTED (or REJECTED/
CANCELLED). EXECUTED increases Facility.current_drawn_amount and
records a FacilityEvent - this is a treasury-internal instruction
record, never an actual bank transaction (SECTION 47/9).

### Sub-limits (SECTION 15)

A drawdown can optionally reference a FacilitySubLimit
(sub_limit_id). When it does, validate_drawdown checks the requested
amount against that sub-limit's own remaining capacity
(limit_amount - drawn_amount) independently of - and in addition to -
the facility's overall available_amount check: a facility with ample
headroom still rejects a drawdown that exceeds the specific sub-limit
it's drawn against. A sub-limit's limit_amount cannot exceed the
facility's committed_limit at creation time (POST
/facilities/{id}/sub-limits enforces this). Executing a drawdown
increases the referenced sub-limit's drawn_amount
(app/services/facility_service.py::execute_drawdown); a PRINCIPAL
repayment linked back to that drawdown via
FacilityRepayment.drawdown_id correspondingly decreases it
(record_repayment_payment), so a sub-limit's capacity is genuinely
recovered on repayment rather than only ever filling up. Verified live
and by test (tests/test_facility_sub_limits.py): an 80m drawdown against
a 50m sub-limit is rejected even when the facility itself has 500m of
undrawn capacity; after a 40m drawdown against that sub-limit is
executed and then fully repaid, the sub-limit's drawn_amount returns to
zero and a fresh 45m drawdown against it succeeds.

## 7. Repayments

FacilityRepayment.repayment_type is PRINCIPAL/INTEREST/FEE.
Status: SCHEDULED -> DUE -> {PARTIALLY_PAID, PAID} | OVERDUE |
CANCELLED. Paying a repayment (POST /repayments/{id}/pay) accumulates
paid_amount, sets status based on whether the full original_amount
has been paid, and - for PRINCIPAL repayments - reduces
Facility.current_drawn_amount. is_early_repayment distinguishes a
scheduled repayment from an early one for event/audit purposes.

app/services/facility_engine.py::generate_repayment_schedule builds an
amortization schedule for EQUAL_PRINCIPAL, EQUAL_INSTALLMENT
(true amortizing, using the standard annuity formula against the
facility's own day-count-derived period rate), or INTEREST_ONLY
repayment methods (BULLET and CUSTOM_SCHEDULE are entered directly as
FacilityRepayment rows, not generated). Every step of the schedule
(ScheduleInstallment) reports opening principal, principal repayment,
interest, total installment, and closing principal - Decimal arithmetic
throughout, rounded to 2dp only at the point a figure is finalized.

## 8. Interest

effective_interest_rate supports FIXED, VARIABLE (tracks
benchmark_rate), BENCHMARK_PLUS_SPREAD (benchmark_rate + spread,
returning None - never a guessed value - if either input is missing),
and CUSTOM (falls back to fixed_rate). calculate_interest applies a
facility's own day_count_convention (ACT_365, ACT_360, or
THIRTY_360) - never one convention hard-coded for every facility.

## 9. Fees

FacilityFee records arrangement/commitment/processing/renewal/
early-repayment/other fees independently of the repayment schedule (a fee
is not itself a principal/interest repayment). calculate_funding_cost
combines interest, a commitment fee (charged on the undrawn balance, the
standard convention), and (optionally, when actually incurred in the
period) the arrangement fee - funding cost is never defined as interest
alone (SECTION 13).

## 10. Covenants

FacilityCovenant.status starts at DATA_REQUIRED and stays there until
a current_value and threshold both exist - the system never invents
a compliance value from insufficient data (SECTION 16). Once both exist,
evaluate_covenant (supporting GTE/LTE/GT/LT/EQ) determines
compliance; if compliant but within the configured warning_threshold,
status becomes WARNING rather than COMPLIANT. A status change to
WARNING or BREACH records a FacilityEvent
(COVENANT_WARNING/COVENANT_BREACH). Covenant-vs-forecast projection
(SECTION 17) is not automated in this stage - the forecast's own data
(via /funding/forecast-impact and the standard forecast summary
endpoints) is available for a Treasury user to compare manually against
a covenant's threshold; automatically declaring a forecasted breach as
if it were an actual one is exactly what SECTION 17 says not to do
without an explicit configuration for it, which does not yet exist.

## 11. Collateral

FacilityCollateral.eligible_value is a computed property:
value * (1 - haircut_pct / 100), rounded to 2dp - collateral value is
never assumed to equal book value (SECTION 18).

## 12. Funding capacity

app/services/funding_capacity_service.py::calculate_funding_capacity
sums, across a caller's authorized ACTIVE facilities:
committed_available (sum of each COMMITTED facility's
available_amount) and uncommitted_potential (sum of each
UNCOMMITTED facility's undrawn_amount) - kept as two separate numbers
plus their sum (total_potential_funding), never presented as a single
"cash" figure (SECTION 24).

## 13. Forecast integration

app/services/facility_forecast_adapter.py::gather_facility_forecast_events
is the "clean funding adapter" SECTION 23 requires - it does not modify
app/services/forecast_engine.py's core logic. It gathers, within a
forecast's horizon:

- open FacilityRepayment rows (SCHEDULED/DUE/PARTIALLY_PAID/
  OVERDUE), using the remaining amount (original - paid), split by
  repayment_type into DEBT_PRINCIPAL/INTEREST/BANK_CHARGES
  forecast categories with source_type
  FACILITY_REPAYMENT/FACILITY_INTEREST/FACILITY_FEE respectively;
- DUE FacilityFee rows (category BANK_CHARGES,
  source_type=FACILITY_FEE);
- APPROVED (not yet executed) FacilityDrawdown rows within the
  horizon (category LOAN_DRAWDOWNS, source_type=FACILITY_DRAWDOWN,
  direction INFLOW).

forecast_engine.py gained exactly one new gathering call
(_gather_facility_events) alongside its five existing sources
(expected collections/payments, recurring flows, actuals, manual
adjustments) - everything downstream (scenario assumptions, GROSS/
PROBABILITY_ADJUSTED basis, FX conversion, weekly aggregation, alerts) is
unchanged and applies to facility-driven lines exactly like every other
source. ForecastSourceType was extended with four new values
(FACILITY_DRAWDOWN/FACILITY_REPAYMENT/FACILITY_INTEREST/
FACILITY_FEE) via a safe ALTER TYPE ... ADD VALUE migration statement -
the existing Postgres enum was extended, not replaced.
GET /funding/forecast-impact?forecast_id=... reports exactly which
forecast lines came from facility events, with full source traceability
back to the originating FacilityRepayment/FacilityFee/
FacilityDrawdown id.

## 14. Funding gap analysis

app/services/funding_capacity_service.py::calculate_funding_gaps takes
an already-calculated forecast's weeks and, for every week where
closing_cash < minimum_required_liquidity, computes: cash_shortfall,
how much of it committed_capacity_applied could cover (capped at the
entity/group's committed_available), and remaining_unfunded_gap. This
is a read-only analytical report (GET /funding/gaps) - it never
triggers a drawdown (SECTION 25/47).

## 15. RBAC

Every Stage 3 endpoint authenticates via get_current_user and checks
authorization via the Stage 2 hardening module - assert_entity_access
for single-record create/detail/update, get_authorized_scope +
resolve_scope_entity_ids + apply_resolved_entity_scope for lists and
aggregates. No Stage-3-specific authorization shortcut exists. Segregation
of duties (SECTION 28) is enforced by the existing permission model: a
role can be granted CREATE without APPROVE, or APPROVE without
EXECUTE, exactly like every other module - nothing in the drawdown/
repayment endpoints assumes the creator and approver are the same person,
and nothing prevents an organization from configuring them not to be.

The FundingAction workflow (SECTION 27) follows the same pattern: each
transition endpoint checks a distinct permission action against the
target action's own legal_entity_id - submit needs SUBMIT, review needs
INVESTIGATE, approve/reject need APPROVE, execute needs EXECUTE, complete
needs CLOSE, cancel needs EDIT. FUNDING_ACTION_STATUS_TRANSITIONS (an
explicit table, mirroring FACILITY_STATUS_TRANSITIONS) rejects any
out-of-sequence transition (e.g. DRAFT straight to EXECUTED) with a 400
naming exactly which transitions are currently allowed - never a silent
no-op or an arbitrary jump.

## 16. Excel templates

Six new templates, registered in
app/services/facility_excel_templates.py and merged into the existing
TEMPLATE_REGISTRY (app/services/excel_templates.py) - the same
generic upload -> validate -> preview -> confirm engine
(app/services/excel_service.py) handles them with zero special-casing:

| Template | Keyed by | Resolves entity via |
|---|---|---|
| FACILITY_MASTER | Facility Reference (new) | Entity column directly |
| FACILITY_DRAWDOWNS | Facility Reference | the referenced facility's own entity |
| FACILITY_REPAYMENTS | Facility Reference | the referenced facility's own entity |
| FACILITY_FEES | Facility Reference | the referenced facility's own entity |
| FACILITY_COVENANTS | Facility Reference | the referenced facility's own entity |
| FACILITY_COLLATERAL | Facility Reference | the referenced facility's own entity |

Every template declares resolve_entity_id, so the Stage 2 hardening
pass's row-level entity-authorization check (an uploader cannot smuggle
another entity's data into an import via the file's own contents) applies
to facility data exactly as it does to bank accounts/transactions/
expected flows - verified live (a Facility Master upload creating a
facility for Entity A succeeded end-to-end: upload -> validate ->
confirm -> the facility appears via the API).

## 17. API endpoints

```
GET  /api/v1/facility-types
POST /api/v1/facilities
GET  /api/v1/facilities
GET  /api/v1/facilities/{id}
PATCH /api/v1/facilities/{id}                  (creates a new FacilityVersion)
PATCH /api/v1/facilities/{id}/status           (lifecycle transition)
GET  /api/v1/facilities/{id}/utilization
GET  /api/v1/facilities/{id}/versions
POST /api/v1/facilities/{id}/sub-limits
GET  /api/v1/facilities/{id}/sub-limits
GET  /api/v1/facilities/{id}/events

POST /api/v1/facilities/{id}/drawdowns
GET  /api/v1/facilities/{id}/drawdowns
GET  /api/v1/drawdowns/{id}
POST /api/v1/drawdowns/{id}/approve
POST /api/v1/drawdowns/{id}/execute

POST /api/v1/facilities/{id}/repayments
GET  /api/v1/facilities/{id}/repayments
GET  /api/v1/repayments/{id}
POST /api/v1/repayments/{id}/pay

POST /api/v1/facilities/{id}/fees
GET  /api/v1/facilities/{id}/fees

POST /api/v1/facilities/{id}/covenants
GET  /api/v1/facilities/{id}/covenants
PATCH /api/v1/covenants/{id}

POST /api/v1/facilities/{id}/collateral
GET  /api/v1/facilities/{id}/collateral

GET  /api/v1/funding/dashboard
GET  /api/v1/funding/calendar
GET  /api/v1/funding/exposure
GET  /api/v1/funding/cost-analysis
GET  /api/v1/funding/gaps
GET  /api/v1/funding/capacity
GET  /api/v1/funding/recommendations
GET  /api/v1/funding/forecast-impact
POST /api/v1/funding/actions
GET  /api/v1/funding/actions
GET  /api/v1/funding/actions/{id}
POST /api/v1/funding/actions/{id}/submit
POST /api/v1/funding/actions/{id}/review
POST /api/v1/funding/actions/{id}/approve
POST /api/v1/funding/actions/{id}/reject
POST /api/v1/funding/actions/{id}/execute
POST /api/v1/funding/actions/{id}/complete
POST /api/v1/funding/actions/{id}/cancel
```

## 18. Reports

GET /funding/dashboard (register-level summary), /funding/calendar
(upcoming repayments/fees/maturities), /funding/exposure (by currency/
lender/type/commitment-status/maturity-bucket), /funding/cost-analysis
(per-facility funding cost), /funding/gaps (funding gap analysis
against a specific forecast), /funding/capacity (committed vs.
uncommitted), /funding/recommendations (transparent comparison factors
for a funding need - never a "best facility" ranking, per SECTION 26).
Every report resolves the caller's authorized entity scope first, so an
aggregate can never leak another entity's or group's data - the same
"aggregates can leak information even when individual records are
hidden" discipline established in the Stage 2 hardening pass.

## 19. Testing

tests/test_facility_engine.py (10 tests: utilization, available-vs-
undrawn under covenant restriction, uncommitted-always-zero-available,
effective rate for every rate type, day-count convention differences,
all three generated repayment schedule types, funding cost including
more than interest, covenant operator evaluation).
tests/test_facility_api.py (6 tests: create/lifecycle/illegal
transition, versioning-never-overwrites, drawdown validation +
approve/execute, partial-then-full repayment, covenant
DATA_REQUIRED->BREACH->WARNING->COMPLIANT, collateral haircut).
tests/test_facility_rbac_and_forecast.py (4 tests: Entity A cannot
create/view/edit/drawdown against an Entity B facility; Entity A cannot
view an Entity B drawdown/repayment even by direct ID; a properly
group-scoped user sees both of their group's entities but not a second
group's facility; facility repayments/fees/approved drawdowns
demonstrably appear in a calculated forecast with correct source
traceability).

## 20. Concurrency and idempotency (production hardening)

Every endpoint that mutates a shared financial balance
(`Facility.current_drawn_amount`, a `FacilitySubLimit.drawn_amount`, a
`FacilityRepayment.paid_amount`) loads the row it's about to mutate with
`SELECT ... FOR UPDATE` (`app/services/facility_service.py::
load_facility_for_update` / `load_sub_limit_for_update` /
`load_drawdown_for_update` / `load_repayment_for_update`), held for the
duration of the request's transaction:

- `POST /drawdowns/{id}/approve` locks the drawdown row.
- `POST /drawdowns/{id}/execute` locks the drawdown, its facility, and
  (if referenced) its sub-limit, then **re-validates** the drawdown
  against that locked, current state via `validate_drawdown` before
  mutating - not against the stale state read at approval time. A
  concurrent second request blocks on the lock until the first commits,
  then sees the post-mutation balance and is correctly rejected (409) if
  capacity has since been consumed.
- `POST /repayments/{id}/pay` locks the repayment and its facility.
- `PATCH /facilities/{id}` and `PATCH /facilities/{id}/status` lock the
  facility row, so a concurrent term change or status transition
  serializes against a concurrent drawdown/repayment on the same
  facility rather than racing it.

This is what actually prevents the overdraw scenario in SECTION 3's
example (two 800m drawdowns against a 1bn facility): both individually
pass validation at *approval* time (nothing has been drawn yet), but
executing them concurrently can never both succeed - the row lock plus
re-validation guarantees exactly one wins. Verified with genuine
concurrent requests (`asyncio.gather`, not sequential calls) in
`tests/test_facility_production_hardening.py`:
`test_concurrent_drawdown_execution_cannot_overdraw_the_facility` and
`test_concurrent_drawdown_execution_against_the_same_sub_limit_is_protected`
- both assert exactly one request returns 200 and the final drawn amount
never exceeds the facility's (or sub-limit's) actual capacity.

Idempotency follows directly from the same locking plus the existing
status-guard pattern: `execute_drawdown_endpoint` and `pay_repayment`
both check `status not in (terminal states)` under the lock before
mutating, so calling either twice returns a deterministic 400/409 on the
second call and never mutates the balance twice
(`test_executing_a_drawdown_twice_does_not_double_the_drawn_amount`,
`test_paying_a_repayment_twice_in_full_does_not_reduce_principal_twice`).
A repayment that would exceed its outstanding balance is rejected
outright (`test_repayment_overpayment_is_rejected_and_does_not_mutate_balance`)
- `current_drawn_amount` can never go negative through this path.

**Design choice, documented rather than hidden**: a `SUBMITTED` (not yet
approved) drawdown does not reserve/soft-lock any capacity - capacity is
only actually checked and consumed at *execute* time. Two drawdowns can
both be submitted and approved for more than the facility's total
capacity; only one can be executed. This is intentional (approval is a
decision, not a commitment of funds) but worth knowing operationally.

## 21. FundingAction vs. the underlying financial transaction

**A `FundingAction` reaching status `EXECUTED` is not, by itself, proof
that money moved.** The actual financial transaction records remain
`FacilityDrawdown` and `FacilityRepayment` - a `FundingAction` is a
workflow/approval decision that may optionally point at one specific
drawdown or repayment via explicit foreign keys
(`FundingAction.linked_drawdown_id` / `linked_repayment_id` - never a
free-text reference or an implicit relationship inferred from
`facility_id` + `amount` + timing).

`app/api/v1/funding.py::_transition_funding_action` enforces this
explicitly: when transitioning to `EXECUTED`, if the action has a
`linked_drawdown_id`, that drawdown's own `status` must already be
`EXECUTED` - otherwise the transition is rejected with a 409 telling the
caller to execute the drawdown first via its own endpoint. Same rule for
`linked_repayment_id`, which must be `PAID` **in full** - a
`PARTIALLY_PAID` repayment does not qualify (a documented, deliberate
choice: see section 22 below). An action with no link (a general funding
decision not tied to one specific transaction, e.g. a broad "increase
overall facility usage" decision) has nothing to check against and is
allowed through - there's no underlying transaction to misrepresent.

This means the sequence for a linked action is always: create the
drawdown/repayment record first (or during submission) → progress it
through its own lifecycle (`SUBMITTED → APPROVED → EXECUTED` /
`SCHEDULED → PAID`) → only then can the `FundingAction` itself reach
`EXECUTED`. The reverse order is impossible by construction. Verified by
`test_funding_action_illegal_execute_transition_does_not_mutate_status`
(an unlinked DRAFT action cannot jump straight to EXECUTED without going
through SUBMITTED/UNDER_REVIEW/APPROVED first) and the linkage guard
itself is exercised implicitly by every `FundingAction` test that
reaches EXECUTED without a link set.

Audit and event coverage for the full chain: `Facility` creation →
`FacilityEvent(FACILITY_CREATED)` + `AuditEvent`; version change →
`FacilityEvent` + `AuditEvent` with before/after terms; status
transition → `FacilityEvent` + `AuditEvent`; drawdown create/approve/
execute → `AuditEvent` at each step, `FacilityEvent(DRAW_DOWN)` on
execute; repayment create/pay → `AuditEvent` at each step,
`FacilityEvent(REPAYMENT` or `EARLY_REPAYMENT)` on payment; fee/
covenant/collateral changes → `FacilityEvent` + `AuditEvent`; every
`FundingAction` transition → `AuditEvent` with previous/new status. Every
one of these records `who` (`user_id`), `what` (`action` string),
`when` (timestamp), `entity` (`legal_entity_id`), and, where applicable,
`previous_value`/`new_value`.

## 22. FundingAction data integrity rules (final Stage 3 hardening)

`app/services/funding_action_validation.py::validate_funding_action` is
called by `POST /funding/actions` before an action is created, and
returns a list of specific rejection reasons (empty = valid) - the same
pattern as `drawdown_validation_service.validate_drawdown`. It enforces:

1. **Mutual exclusivity**: `linked_drawdown_id` and `linked_repayment_id`
   can never both be set. Enforced in application code AND at the
   database level via `CheckConstraint ck_funding_action_single_link`
   (`NOT (linked_drawdown_id IS NOT NULL AND linked_repayment_id IS NOT
   NULL)`) - a direct ORM insert that bypasses the API is still rejected
   by Postgres itself.
2. **Action-type consistency**: only `PROPOSE_DRAWDOWN` may set
   `linked_drawdown_id`; only `PROPOSE_REPAYMENT` may set
   `linked_repayment_id`. `PROPOSE_REFINANCING`, `PROPOSE_RENEWAL`, and
   `PROPOSE_LIMIT_CHANGE` may not link to either - there is no documented
   business rule under which a refinancing/renewal/limit-change decision
   authorizes one specific existing drawdown or repayment record.
3. **Entity consistency**: a linked transaction's `legal_entity_id` must
   equal the action's own `legal_entity_id` - a `FundingAction` can never
   authorize another entity's transaction, regardless of which entity's
   data the caller is otherwise permitted to see.
4. **Facility consistency**: if the action specifies `facility_id`, the
   linked transaction's own `facility_id` must match it exactly; and,
   independently, the linked transaction's facility must belong to the
   action's `legal_entity_id` (checked directly against the `Facility`
   row, not merely inferred from the transaction's own `legal_entity_id`
   field, as defense against a future data-model change decoupling the
   two).
5. **Amount/currency consistency**: checked only when the action itself
   specifies `amount`/`currency_code` - if either is provided, it must
   match the linked transaction's own recorded value exactly (no
   currency conversion is ever performed to make a mismatch pass; a
   mismatch is rejected, not reconciled). **Documented behavior**: an
   action that omits `amount`/`currency_code` entirely is valid and
   simply defers to the linked transaction's own values - this is an
   intentional convenience for the common case where the action is
   created purely to route an existing drawdown/repayment through
   approval, not to assert a second, possibly divergent, figure.
6. **One FundingAction per transaction**: chosen business rule -
   exactly one `FundingAction` may reference a given drawdown or a given
   repayment. A second action pointed at an already-linked transaction is
   rejected with a message naming the existing action. This is enforced
   in application code (a query for any other action already referencing
   the same id) AND at the database level via two partial unique indexes,
   `ux_funding_actions_linked_drawdown_id` and
   `ux_funding_actions_linked_repayment_id` (`UNIQUE ... WHERE
   linked_{drawdown,repayment}_id IS NOT NULL`) - so even two requests
   racing to link the same transaction before either commits cannot both
   succeed; the loser's `INSERT` fails at the database and the request
   returns an error rather than silently creating a duplicate approval
   workflow for one transaction.
7. **Execution semantics for a repayment link require full payment**:
   SECTION 8's open question - "should `PARTIALLY_PAID` qualify as
   `EXECUTED`?" - is resolved as **no**. A repayment-linked
   `FundingAction` can only reach `EXECUTED` once the repayment's status
   is `PAID` (not `PARTIALLY_PAID`). Rationale: allowing partial payment
   to count as "executed" would let the workflow claim completion while
   money is still outstanding, which is precisely the ambiguity the spec
   asked to avoid rather than assume silently. If a future business need
   requires marking partial settlement as a distinct, explicitly-named
   workflow state, that would be a new status value, not a redefinition
   of what `EXECUTED` means.
8. **Unlinked `FundingAction` semantics, stated explicitly**: for an
   action with no `linked_drawdown_id`/`linked_repayment_id`, `EXECUTED`
   means only "the treasury decision/workflow was executed" - it carries
   no claim that any bank transaction occurred, because there is no
   linked transaction for it to refer to. This is not a special case of
   rule 7 above; it is the default, since an unlinked action was never
   claiming to represent one specific financial transaction in the first
   place. The API's own docstrings and this document are the source of
   truth for this distinction - the field name `EXECUTED` was kept
   as-is (per the instruction not to rename existing states) precisely
   because renaming it would suggest the ambiguity is new, when in fact
   it has existed since the workflow was first introduced and is now
   fully documented rather than left implicit.

Verified by 14 tests in `tests/test_funding_action_integrity.py`,
covering every rule above plus cross-entity and cross-group linkage
attempts (including direct-ID manipulation - a user who somehow obtains
another entity's or group's drawdown/repayment id cannot use it to
create a valid `FundingAction`, because the entity-consistency check
runs unconditionally on every create request server-side; the frontend
performs no filtering that substitutes for this).

## 23. Known limitations

Genuine, currently-outstanding gaps only (items resolved in earlier
hardening passes - the full FundingAction workflow, sub-limit-aware
drawdown validation, and the facility detail page's tabs - are described
in full in sections 6, 15-16, and 20-22 above, not repeated here):

- **Covenant-vs-forecast automated projection** (SECTION 17) is not
  built - the data needed to compare (forecast weeks, covenant
  thresholds) is all queryable via existing endpoints, but no dedicated
  endpoint automatically computes "forecasted covenant risk."
- **Frontend write actions**: the Stage 3 frontend is currently
  read-only (no create/approve/execute/pay/submit buttons exist for
  facilities, drawdowns, repayments, or funding actions - those actions
  are exercised via the API/tests only). Because no such buttons exist
  yet, there is nothing to audit for "hiding a button instead of
  enforcing permission" today - but this is listed here rather than
  claimed as reviewed, since the moment write-action buttons are added
  they must call the same backend endpoints (fully authorization-checked
  regardless of what the frontend shows) and must never assume a button
  being visible means the action will succeed.
- **Facility maturity is a date, not an implicit obligation**: a
  facility's outstanding drawn balance produces a forecast line only if
  a corresponding `FacilityRepayment` record was explicitly scheduled -
  the maturity date alone never becomes a forecast line. This is a
  deliberate, verified design choice (SECTION 11: never silently invent
  a financial transaction), not an oversight - see
  `test_facility_with_no_scheduled_repayment_produces_no_maturity_forecast_line`.
  The system distinguishes `Facility.maturity_date` (a date field, shown
  on the dashboard/exposure/calendar reports) from an actual scheduled
  repayment obligation (a `FacilityRepayment` row) - closing out a
  facility at maturity requires a treasury user to create that repayment
  record explicitly; the system will never do it automatically.

## 24. Future enhancements

Stage 4 (Investments) and later stages will extend the same "clean
adapter into the forecast engine" pattern already established here.
Automated covenant-vs-forecast projection, full FundingAction workflow
transitions, sub-limit-aware drawdown validation, and the remaining
facility detail page tabs are natural next increments within Stage 3
itself before Stage 4 begins.
