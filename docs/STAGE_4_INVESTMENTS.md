# Stage 4 - Investments & Fixed Deposit Management

## 1. Purpose

Gives Treasury a complete record of invested funds - starting with Fixed
Deposits, architected to extend to other instruments later - and
connects that record to the 13-Week Cash Flow Forecast Engine, so
scheduled maturities become real forecast drivers. The module keeps
"cash at bank" and "invested funds" strictly distinct throughout
(SECTION 21), tracks the full approval-to-placement-to-maturity
lifecycle, and never invents a financial transaction the business hasn't
actually recorded.

## 2. Architecture

Deliberately mirrors the Stage 3 Facility pattern, since it is this
codebase's established, already-hardened convention for "an instrument
with versioned commercial terms plus a separate financial-event ledger":

```
InvestmentType (configurable lookup)
   |
Investment (master: current terms, status, outstanding principal)
   |
InvestmentVersion (versioned terms - never overwritten)
   |
InvestmentTransaction (the financial-event ledger: placement, interest
   |                    accrual/receipt, termination, rollover,
   |                    rebooking, penalty, maturity settlement -
   |                    SECTION 10: the master is NEVER itself the cash
   |                    movement)
   |
InvestmentEvent (append-only, business-readable timeline)
   |
13-WEEK FORECAST (via investment_forecast_adapter.py, the same "clean
                   adapter" pattern as facility_forecast_adapter.py)
```

No new authorization model was introduced. Every Stage 4 endpoint uses
the same app/auth/authorization.py module built during the Stage 2
hardening pass. TreasuryAction gained four new verbs (PLACE,
TERMINATE, ROLLOVER, REBOOK) via a dedicated migration - the same
permission framework, never a parallel one.

## 3. Investment types

InvestmentType is a configurable lookup table (like FacilityType) -
not a fixed enum. Seeded with FIXED_DEPOSIT (is_implemented=True)
plus CALL_DEPOSIT, MONEY_MARKET, TREASURY_BILL, BOND,
COMMERCIAL_PAPER, OTHER (all is_implemented=False) - present so the
architecture is visibly extensible without faking functionality for
instruments that don't exist yet (SECTION 3/45). Only Fixed Deposit has
working placement/termination/rollover/rebooking logic in Stage 4.

## 4. Fixed deposit terms

Original commercial terms (principal, rate, dates, tenor, interest
method, termination/rollover permissions, penalty terms) are captured on
creation and snapshotted into InvestmentVersion version 1
(record_initial_version). Any material amendment (via PATCH
/investments/{id} while still DRAFT, or rebook) creates a new
version - the prior one is never edited, mirroring FacilityVersion.

## 5. Rate types

InvestmentRateType: FIXED, VARIABLE, NEGOTIATED, CUSTOM. For
Fixed Deposits, FIXED and NEGOTIATED are the primary cases per
SECTION 5; rate_source records where a negotiated rate came from. A
historical rate is never silently changed - a rate change is a
rebook/apply_investment_update call that creates a new version.

## 6. Interest calculation

app/services/investment_engine.py::calculate_simple_interest -
principal x annual_rate x day_count_fraction, supporting ACT_365,
ACT_360, 30_360. A pure function of its explicit inputs only (never
reads a mutable Investment row), so a historical calculation is
reproducible exactly regardless of any later amendment - verified by
test_simple_interest_is_pure_and_reproducible. Uses Decimal
throughout; never float.

The math itself is deliberately re-implemented here rather than imported
from app.services.facility_engine - the two modules happen to share the
same day-count-fraction formula, but Stage 4 has no hard dependency on
Stage 3's enum lifecycle (each has its own DayCountConvention enum,
with a distinct Postgres enum type name,
investment_day_count_convention, to avoid a type-name collision).

## 7. Interest payment methods

InterestPaymentMethod: AT_MATURITY, PERIODIC, UPFRONT.
InterestPaymentFrequency (MONTHLY/QUARTERLY/SEMI_ANNUAL/
ANNUAL/CUSTOM) applies when PERIODIC is selected. Investment
tracks expected_interest (computed via calculate_simple_interest at
creation/rebooking time), accrued_interest, and received_interest as
three distinct fields - never conflated.

## 8. Investment lifecycle

InvestmentStatus: DRAFT -> SUBMITTED -> UNDER_REVIEW -> APPROVED ->
PLACEMENT_PENDING -> ACTIVE -> {MATURED, PARTIALLY_TERMINATED,
TERMINATED, ROLLED_OVER, REBOOKED}, plus REJECTED/CANCELLED. An
explicit transition table (INVESTMENT_STATUS_TRANSITIONS, the same
discipline as FACILITY_STATUS_TRANSITIONS) rejects any out-of-table
transition with a 400 naming exactly which transitions are currently
allowed - verified by test_investment_lifecycle_illegal_transition_rejected
(DRAFT cannot jump straight to ACTIVE).

## 9. Investment approval vs. placement

The system distinguishes the approval decision (DRAFT -> ... ->
APPROVED -> PLACEMENT_PENDING, driven by PATCH
/investments/{id}/status) from actual placement (POST
/investments/{id}/place, which alone creates the PLACEMENT
InvestmentTransaction and moves status to ACTIVE). Creating an
investment never places it - the two are separate, explicit actions,
exactly like Stage 3's FacilityDrawdown create-vs-execute split.

## 10. Investment transactions

InvestmentTransaction.transaction_type: PLACEMENT,
INTEREST_ACCRUAL, INTEREST_RECEIPT, PARTIAL_TERMINATION,
FULL_TERMINATION, ROLLOVER, REBOOKING, PENALTY,
MATURITY_SETTLEMENT. Every financial event on an investment is one of
these explicit rows - the Investment master's principal_amount is
updated as a side effect of a transaction, never mutated on its own.

## 11. Placement

app/services/investment_validation_service.py::validate_placement
checks, before placement: status is APPROVED or PLACEMENT_PENDING;
principal is positive; maturity is not before start; the source account
(if set) belongs to the investment's own entity; placement date is not
after maturity; and, when the caller supplies a known available-cash
figure, principal does not exceed it. POST /investments/{id}/place is
row-locked and idempotent: place_investment raises 409 if a
PLACEMENT transaction already exists for the investment - verified by
test_placement_is_idempotent and, under real concurrency, by
test_concurrent_placement_cannot_place_twice.

## 12. Cash impact

Placement is represented as a PLACEMENT InvestmentTransaction
(SECTION 12) - a real row in the internal treasury data model, not just
a displayed relationship. This is exactly what
investment_forecast_adapter.py reads for planned (not-yet-executed)
placements still in PLACEMENT_PENDING status with a future
placement_date, so a planned placement's cash-outflow impact is
visible in the 13-week forecast before it actually happens.

## 13. Maturity

The system distinguishes Investment.maturity_date (a date field, shown
on the dashboard/calendar/forecast) from an actual settlement
transaction. Reaching the maturity date does NOT by itself create a
MATURITY_SETTLEMENT transaction or change status to MATURED - that
remains a deliberate workflow action (status transition to MATURED, or
a termination/rollover at or after maturity), never automatic. The
forecast adapter reports the scheduled maturity (principal + expected
interest, from the investment's own outstanding balance) as a forecast
line, clearly distinct from a settled transaction.

## 14. Early termination

POST /investments/{id}/terminate supports full and partial early
termination. Validates (validate_termination_amount): investment is
ACTIVE/PARTIALLY_TERMINATED; early termination is permitted
(early_termination_allowed); partial termination is permitted when the
amount is less than the outstanding principal
(partial_termination_allowed); amount is positive and does not exceed
outstanding principal. calculate_early_termination
(investment_engine.py) computes principal returned, interest actually
earned to the termination date (never a to-maturity figure the
investment never reached), penalty (per PenaltyType:
FLAT_AMOUNT/RATE_REDUCTION/FORFEIT_INTEREST/NONE), and net
proceeds. The original InvestmentVersion history is untouched; an
InvestmentEvent and InvestmentTransaction record the termination.

## 15. Partial termination

Example (matches SECTION 15's worked example, verified by
test_partial_then_full_termination): a 500m investment partially
terminated for 150m leaves 350m outstanding, status
PARTIALLY_TERMINATED; the original placement transaction and version
history remain intact; a further full termination of the remaining 350m
correctly reaches 0 outstanding and status TERMINATED. Concurrency:
two concurrent partial terminations that together would exceed the
outstanding principal can never both succeed - row-locked
re-validation against the current (not stale) balance guarantees
exactly one wins (test_concurrent_partial_terminations_cannot_exceed_outstanding_principal).

## 16. Rollover

POST /investments/{id}/rollover creates a genuinely new
Investment row for the rolled amount - the original's own rate,
maturity, and terms are never overwritten (SECTION 16). Explicit
lineage via previous_investment_id / rolled_to_investment_id. A full
rollover (amount == outstanding principal) closes the original as
ROLLED_OVER; a partial rollover reduces the original's principal and
leaves it PARTIALLY_TERMINATED for the remainder - verified by
test_rollover_creates_new_investment_preserves_original. Idempotent
by construction: the endpoint re-validates the requested amount against
the row-locked, current outstanding principal, so a concurrent second
rollover attempt against the same (already-reduced/closed) investment
is rejected before a second replacement investment can be created
(test_concurrent_rollover_creates_only_one_replacement_investment).

## 17. Rebooking

POST /investments/{id}/rebook amends the SAME investment's terms
(unlike rollover, which creates a new record) via
apply_investment_update - a new InvestmentVersion, never a silent
overwrite. Additional principal is recorded as its own REBOOKING
transaction. POST /investments/{id}/compare-rollover provides
transparent comparison factors (current rate, proposed rate, current/
proposed maturity, additional/withdrawn principal, expected interest
under each term, penalty, net expected proceeds) via
compare_rollover_options - the result dataclass has no "recommended"/
"best"/"score" field at all (verified by
test_compare_rollover_options_never_ranks); Treasury makes the
decision.

## 18. Investment versioning

InvestmentVersion mirrors FacilityVersion exactly: one row per
material change, immutable once created. There is no endpoint that
accepts a write to a version by id - verified by
test_historical_investment_version_cannot_be_mutated_through_the_api
(a PATCH to a version resource 404s, since no such route exists) and,
separately, that two subsequent changes (a repricing, then a rollover)
leave every earlier version's snapshot untouched when re-fetched.

## 19. Investment events

InvestmentEvent records who (created_by_user_id), what
(event_type), when (event_date), amount/currency where applicable,
previous/new value where applicable, and a free-text description/reason
- for creation, submission, approval, rejection, placement, terms
changes, interest accrual/receipt, partial/full termination, maturity,
rollover, rebooking, penalty, and cancellation. Distinct from the
general AuditEvent table, which every material action also writes to
(SECTION 38) with who/what/when/entity/investment/before/after/reason.

## 20. Forecast integration

app/services/investment_forecast_adapter.py::gather_investment_forecast_events
is the "clean adapter" this codebase's convention requires - it does not
modify forecast_engine.py's core logic. Gathers, within a forecast's
horizon: scheduled maturities for ACTIVE/PARTIALLY_TERMINATED
investments (principal and expected interest as two separately traceable
lines, category INVESTMENT_MATURITIES, source_type
INVESTMENT_MATURITY_PRINCIPAL/INVESTMENT_MATURITY_INTEREST), and
planned future placements for PLACEMENT_PENDING investments (category
INVESTMENT_PLACEMENTS, source_type=INVESTMENT_PLACEMENT, OUTFLOW).
forecast_engine.py gained exactly one new gathering call
(_gather_investment_events) alongside the existing facility one -
everything downstream (scenario assumptions, FX conversion, weekly
aggregation) is unchanged. ForecastSourceType was extended with five
new values via a safe ALTER TYPE ... ADD VALUE migration. Verified by
test_investment_maturity_appears_in_forecast_with_traceability_and_no_duplication:
a forecast recalculated twice produces exactly one principal line and
one interest line per investment, both traceable back to the originating
investment id, with the exact expected amounts.

A facility with no scheduled repayment produces no forecast line at
maturity (Stage 3's rule); the investment equivalent is different by
design - since an ACTIVE investment's principal_amount and
expected_interest are already known, deterministic figures (not an
invented obligation like a facility's undetermined repayment amount
would be), the scheduled maturity is safely and correctly forecast
directly from the investment's own outstanding balance.

## 21. Cash/liquidity treatment

GET /investments-reports/liquidity (SECTION 21/22) reports only
invested amounts and their maturity schedule - total_invested,
maturing_7/30/90_days, total_expected_interest,
weighted_average_rate, broken down by_currency/by_entity/
by_institution. It never reports or implies "available cash" - an
investment that cannot be accessed immediately is never presented as
immediately available. The existing Cash & Liquidity module (Stage 1/2)
remains the sole source of actual bank-account cash figures; this
endpoint is deliberately scoped to invested funds only, so the two are
never combined into one number by any single endpoint.

## 22. Concentration

GET /investments-reports/concentration reports current exposure by
institution, currency, and investment type against
InvestmentConcentrationLimit rows (configurable, never hard-coded -
SECTION 23). A dimension with no configured limit reports
NO_LIMIT_CONFIGURED; with a limit, WITHIN_LIMIT/WARNING/BREACH
based on the limit's own warning_threshold_pct. Verified by
test_concentration_breach_status_against_configured_limit (a 600m
placement against a 500m institution limit correctly reports BREACH
with the exact current/limit amounts).

## 23. RBAC

Every Stage 4 endpoint authenticates via get_current_user and checks
authorization via the Stage 2 hardening module -
assert_entity_access for single-record operations,
get_authorized_scope + resolve_scope_entity_ids +
apply_resolved_entity_scope for lists and aggregates. TreasuryAction
gained PLACE, TERMINATE, ROLLOVER, REBOOK (migrated via ALTER
TYPE ... ADD VALUE) so segregation of duties can be configured
per-action exactly like every other module (SECTION 28) - nothing
assumes the creator/approver/executor of an investment action are the
same person. Verified extensively: an entity-scoped user cannot create,
view, list, terminate, or query the liquidity of another entity's
investment (403 in every case); a group-scoped user sees only their own
group's entities, never a second group's investment, including by direct
ID (test_entity_a_cannot_see_or_modify_entity_b_investment,
test_cross_group_investment_access_is_blocked).

## 24. Excel Data Hub

Five new templates registered in
app/services/investment_excel_templates.py, merged into the existing
TEMPLATE_REGISTRY - the same generic upload -> validate -> preview ->
confirm engine handles them with zero special-casing:

| Template | Keyed by | Resolves entity via |
|---|---|---|
| INVESTMENT_MASTER | Investment Reference (new) | Entity column directly |
| INVESTMENT_PLACEMENTS | Investment Reference | the referenced investment's own entity |
| INVESTMENT_INTEREST | Investment Reference | the referenced investment's own entity |
| INVESTMENT_TERMINATIONS | Investment Reference | the referenced investment's own entity |
| INVESTMENT_ROLLOVERS | Original Investment Reference | the referenced original investment's own entity |

Every template declares resolve_entity_id, so the Stage 2 hardening
pass's row-level entity-authorization check applies to investment data
exactly as it does to facility data - verified live and by test
(test_investment_master_excel_import_rejects_unauthorized_entity_rows:
a mixed-entity INVESTMENT_MASTER upload imports only the row for the
entity the uploader is authorized for; the other row is rejected during
confirm, not silently imported).

## 25. Audit

Every material investment action calls record_audit_event in addition
to writing an InvestmentEvent: creation, editing (while DRAFT),
status transitions, placement, termination, rollover, and rebooking all
have audit coverage with who/what/when/entity/investment/reason and,
where applicable, previous/new values.

## 26. Concurrency and idempotency

The same discipline as Stage 3's final hardening pass:
load_investment_for_update (SELECT ... FOR UPDATE) is used by every
endpoint that mutates principal_amount or status - placement,
termination, rollover, rebooking, and status transitions. Genuine
concurrent requests (asyncio.gather, not sequential calls) are used in
tests to prove: two concurrent placements on the same investment - only
one succeeds, exactly one PLACEMENT transaction ever exists; two
concurrent partial terminations that together exceed outstanding
principal - exactly one succeeds, remaining principal never goes
negative or below what it should be; two concurrent full-amount
rollover attempts - exactly one creates a new investment, the loser is
rejected (either because the amount no longer fits the now-reduced
principal, or because the original's status has already moved past
rollover eligibility) and no second replacement investment is ever
created.

## 27. Known limitations

- InvestmentTransaction rows of type INTEREST_ACCRUAL are not
  automatically generated by a scheduled job - accrued_interest
  tracking exists on the model and expected_interest is always
  computed, but there is no periodic accrual-posting process in Stage 4;
  interest is realized via INTEREST_RECEIPT (manual or Excel-imported)
  or at maturity/termination.
- PERIODIC interest payment schedules (InterestPaymentFrequency) are
  modeled but there is no automatic schedule generator producing
  individual InvestmentTransaction rows per period - a treasury user
  or Excel import records each interest receipt directly.
- The frontend is read-only for write actions (no create/approve/place/
  terminate/rollover/rebook buttons) - all backend endpoints are fully
  functional and authorization-checked, exercised via the API/tests;
  only the dashboard, list, and a read-only detail page (Overview,
  Commercial Terms, Versions, Transactions, Events) are built in the UI.
- Investment Rate Comparison (SECTION 25) is exposed via
  POST /investments/{id}/compare-rollover (API-only) - no dedicated
  frontend screen for it yet.
- Multi-currency FX conversion for cross-currency liquidity roll-ups is
  not implemented - by_currency in the liquidity view keeps each
  currency's principal separate (never silently summed), matching
  SECTION 26's "do not convert historical amounts using today's rate"
  instruction, but there is no converted-to-reporting-currency total.

## 28. Confirmation

Stage 5 (Bank Reconciliation), Intercompany Reconciliation, the AI
Treasury Copilot, external bank APIs, and open banking were NOT
implemented in this stage.
