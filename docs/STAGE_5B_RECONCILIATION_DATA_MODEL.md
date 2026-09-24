# Stage 5B — Reconciliation Data Model

## 1. Scope

Stage 5B establishes the secure, auditable persistence foundation for
reconciliation - it does NOT determine whether any transaction matches
another. No candidate generation, no scoring, no confidence calculation,
no automatic matching decisions, no one-to-many/many-to-one logic, no
advanced matching (batch payments, internal transfers, FX, bank
charges), no adaptive learning, no open-item workflow (assignment/
aging/resolution beyond storage), and no reconciliation reports exist
after this stage. All of that is explicitly Stage 5C onward.

```
BankStatementTransaction (Stage 5A - external bank evidence)
          |
          v
   ReconciliationRun (Stage 5B - a scoped execution record)
          |
          v
 Match Suggestions / Open Items (Stage 5B tables - EMPTY until 5C+)
```

## 2. Stage 5A freeze verification

Before any Stage 5B work began, the completed Stage 5A implementation
was re-verified unchanged:
- `ImportBatchStatus` (UPLOADED/VALIDATING/VALIDATED/READY_FOR_IMPORT/
  IMPORTING/IMPORTED/PARTIALLY_IMPORTED/FAILED/CANCELLED) already
  matches the required lifecycle exactly - no second status enum was
  created.
- Full backend test suite: 192/192 passing.
- Frontend build: clean, 13/13 pages.
- `alembic heads`: exactly one.
- No cash-ledger side effects: `BankStatementTransaction` remains
  completely separate from `TreasuryTransaction`/`BankBalance`.

Stage 5A was not rewritten; only additively built upon.

## 3. Data model

### `ReconciliationRun`
Scoped strictly to one `legal_entity_id` and one `bank_account_id` -
never group-wide. `group_id` is NOT stored redundantly (derivable via
`legal_entity.group_id`), matching the established convention already
used by every other entity-scoped model (`Investment`, `Facility`,
etc.). Carries `period_start`/`period_end`, `status`, a
`configuration_id` FK (the EXACT configuration version resolved at
creation time - see Section 5), result-count fields
(`statement_transaction_count`/`eligible_transaction_count`, both 0
until execution), `notes`/`failure_reason`, and full actor/timing
fields (`created_by_user_id`/`executed_by_user_id`/`started_at`/
`completed_at`).

### `ReconciliationMatchSuggestion`
Persistence shape for a FUTURE matching engine's output. Both
`bank_statement_transaction_id` and `treasury_transaction_id` are
nullable (a future "no candidate found" placeholder is representable
without inventing a fake counterpart). `match_type`, `confidence`,
`matching_rule_version`, and `weights_version_id` are all reserved,
currently-unused placeholders - **no code in Stage 5B ever inserts a row
into this table**, verified explicitly by
`test_run_creation_never_creates_suggestions_or_open_items`.

### `ReconciliationOpenItem`
Same treatment - persistence shape only, zero rows created by any Stage
5B code path. `legal_entity_id`/`bank_account_id` are denormalized
directly onto the table (not only reachable via the run), matching the
same denormalization convention `InvestmentTransaction`/`FacilityEvent`
already use, so a future RBAC-scoped query never needs an extra join.
`category` uses the full controlled vocabulary from the forensic report
(`BANK_ONLY`, `LEDGER_ONLY`, `AMOUNT_VARIANCE`, `TIMING_DIFFERENCE`,
`DUPLICATE`, `BANK_CHARGE`, `INTERNAL_TRANSFER`, `FX_DIFFERENCE`,
`ONE_TO_MANY`, `MANY_TO_ONE`, `BATCH_PAYMENT`, `MISSING_LEDGER_ENTRY`,
`MISSING_BANK_ENTRY`, `EXCEPTION`) - storage only, no classification
logic exists to assign any of them yet.

### `ReconciliationConfiguration`
Versioned configuration STORAGE only - Stage 5C will define and consume
the actual deterministic matching rules; Stage 5B never reads
`matching_rule_config` to make any decision. Follows `FXRate`'s exact
effective-dated versioning discipline (`version`/`is_current`/
`superseded_by_id`) rather than a new versioning mechanism - a
configuration change is always a NEW row, the prior row is superseded
(never edited in place or deleted), verified by
`test_configuration_versioning_never_overwrites_history`.
`legal_entity_id`/`bank_account_id`/`currency_code` are all nullable:
null means "applies at the broader scope."

## 4. Run status lifecycle

`ReconciliationRunStatus`: `DRAFT`, `READY`, `RUNNING`, `COMPLETED`,
`FAILED`, `CANCELLED`, governed by an explicit transition table
(`RECONCILIATION_RUN_STATUS_TRANSITIONS`) - the same "no arbitrary
status change" discipline as `FACILITY_STATUS_TRANSITIONS`/
`INVESTMENT_STATUS_TRANSITIONS`.

**Design decision**: `POST /reconciliation/runs` creates a run directly
into `READY`, never leaving it in `DRAFT`. Creation-time validation
(entity/bank-account authorization, period ordering, relationship
integrity, configuration resolution) is comprehensive enough by the time
the endpoint succeeds that an intermediate `DRAFT` step would add no
new information the user could still supply. `DRAFT` remains in the
enum, reserved for a future incremental-creation UX (e.g. a multi-step
wizard) that Stage 5B itself does not need or implement.

**`COMPLETED` means the execution completed - never "everything
matched."** A completed run may legitimately contain zero matches and
entirely open items; Stage 5B's own execution boundary creates neither,
so every Stage 5B run's own "matched" count is always zero, by
definition, until Stage 5C exists.

## 5. Configuration resolution and versioning

`resolve_effective_configuration` (`app/services/reconciliation_service.py`)
resolves the applicable configuration when none is explicitly supplied,
most-specific first: exact (entity, account, currency) → (entity,
account) → (entity, currency) → entity-wide → global default. The
resolved configuration's `id` is stored on the run at creation time -
**superseding that configuration later never changes what an existing
run points to**, verified by
`test_run_stores_the_exact_configuration_version_used`. This is exactly
the historical-reproducibility guarantee Section 15 of the Stage 5B spec
requires: "which configuration was used when this reconciliation was
executed?" is always answerable, forever, regardless of later
configuration changes.

## 6. Run creation vs. execution boundary

Creation (`POST /reconciliation/runs`) validates and resolves scope/
configuration eagerly and creates **zero**
`ReconciliationMatchSuggestion`/`ReconciliationOpenItem` rows,
regardless of how much bank statement evidence already exists in scope
- verified explicitly.

Execution (`POST /reconciliation/runs/{id}/execute`) is a strictly
separate operation. `execute_reconciliation_run`
(`app/services/reconciliation_service.py`) does exactly three things:
transitions `READY → RUNNING`, counts `BankStatementTransaction` rows
already within the run's own authorized scope (entity + bank account +
period - never broadened), and transitions `RUNNING → COMPLETED` (or
`FAILED` on an unexpected error, with a durable `failure_reason`).
`eligible_transaction_count` is set equal to
`statement_transaction_count` as an explicit Stage 5B placeholder -
Stage 5C will define real eligibility rules (e.g. excluding rows already
consumed by an earlier run) and refine this. **Nothing here reads
`TreasuryTransaction` for scoring purposes, and nothing here creates a
suggestion or open item** - verified by
`test_execution_creates_no_cash_ledger_side_effects` and
`test_run_creation_never_creates_suggestions_or_open_items`.

Execution revalidates critical conditions at the API layer (not just at
creation time): the bank account must still belong to the run's entity,
and the run's own configuration (if any) must still exist and be
active. Either failure moves the run straight to `FAILED` with a
recorded reason rather than crashing the request.

## 7. Concurrency

`load_run_for_update` (`SELECT ... FOR UPDATE`) row-locks the run before
any lifecycle transition - the exact same discipline already
established for every other stateful workflow in this codebase
(`load_facility_for_update`, `load_investment_for_update`,
`load_import_batch_for_update`). Two concurrent execution requests for
the same run cannot both succeed: the second blocks on the lock, then
observes the already-`RUNNING`/`COMPLETED` status and is rejected (400)
- verified with a genuine concurrent-request test (`asyncio.gather`, not
sequential calls), `test_concurrent_execution_cannot_both_succeed`.

## 8. Security

Every endpoint uses `app.auth.authorization` - no new authorization
mechanism. `TreasuryModule.BANK_RECONCILIATION` (already reserved since
Stage 0) gates every endpoint; `TreasuryAction.CREATE`/`VIEW`/`EDIT`/
`EXECUTE`/`CLOSE`/`CONFIGURE` were all already sufficient - **zero RBAC
enum changes were needed for Stage 5B**.

Verified: an entity-scoped user cannot create a run for another entity
(403), cannot read or execute another entity's run by direct ID (403),
cannot list another entity's runs by filter or find them in an
unfiltered list, and - matching the exact attack pattern Stage 5A's own
security tests established - cannot create a run whose own entity scope
is claimed correctly but whose bank account actually belongs to a
different entity (400, caught server-side regardless of client claims).
Cross-group access is denied the same way.

## 9. Audit

Every material action uses the existing `record_audit_event` - no
second audit system. Run creation, execution start/complete/fail, and
cancellation are all audited with actor, entity, and relevant context
(counts on completion, failure reason on failure).

## 10. API endpoints

`POST /reconciliation/runs`, `GET /reconciliation/runs`,
`GET /reconciliation/runs/{id}`, `PATCH /reconciliation/runs/{id}`
(notes only, DRAFT/READY only), `POST /reconciliation/runs/{id}/execute`,
`POST /reconciliation/runs/{id}/cancel`,
`GET /reconciliation/runs/{id}/suggestions` (always empty in Stage 5B),
`GET /reconciliation/runs/{id}/open-items` (always empty in Stage 5B),
`GET /reconciliation/configurations`, `POST /reconciliation/configurations`.

**Deliberately not implemented**: `POST /reconciliations/auto-match` or
any endpoint that would imply matching exists - per the spec's explicit
warning.

## 11. Frontend

One new page, `/reconciliation` - create a run (entity, bank account,
period), list runs with status, execute/cancel a `READY` run. No
matching dashboard, no candidate comparison UI, no bulk matching, no
open-item assignment workflow, no reconciliation reports - all
explicitly deferred.

## 12. Known limitation found and fixed during testing

`ReconciliationRun.started_at`/`completed_at` and
`ReconciliationOpenItem.resolved_at` were initially declared without
`DateTime(timezone=True)`, causing a timezone-mismatch database error
the first time an execution actually ran. Caught by
`test_run_lifecycle_ready_to_completed`, fixed via a dedicated follow-up
migration (the original migration was never edited, per the established
"never rewrite an applied migration" convention) - re-verified against a
fresh database.

## 13. Explicitly deferred to later sub-stages

- **Stage 5C**: deterministic candidate generation, amount/reference/
  date/party/side scoring, confidence calculation, exact-match rules,
  tolerance matching, persistence of real match suggestions.
- **Stage 5D**: one-to-many, many-to-one, batch payment matching,
  internal transfer detection, FX-aware matching, advanced matching
  rules.
- **Stage 5E**: open-item assignment, escalation, aging management,
  resolution workflow.
- **Stage 5F**: reconciliation reports, four-bucket reporting, report
  versioning/approval/finalization.
- **Stage 5G**: adaptive matching/learning (weights versioning,
  recalibration).
- **Stage 5H**: full reconciliation frontend (matching review,
  exceptions, reports).
- **Stage 5I**: dedicated performance/security hardening pass beyond
  Stage 5B's own tests.
