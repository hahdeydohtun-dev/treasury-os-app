# Stage 5F — Reconciliation Reports

Read-only reporting over the Stage 5A–5E reconciliation data: run summaries, matching performance, open exceptions,
open-item ageing, assignment and investigation, bank-account reconciliation, and entity / group dashboards, with Excel
and PDF exports. Reports never create, change or delete any record except the audit entry describing the request, and
no Stage 5A–5E behaviour was changed (no migration, no change to matching, decisions, claims or the workflow).

## 1. Architecture

| Layer | File | Role |
|---|---|---|
| Calculations | `backend/app/services/reconciliation_report_service.py` | Builds each report as one dictionary: `kpis`, `tables`, `notes`, `definitions`, plus scope and filter metadata. Pure reads. |
| Exports | `backend/app/services/reconciliation_report_export.py` | Renders that **same dictionary** to Excel (`openpyxl`) or PDF (`reportlab`). It never queries the database, so an export cannot differ from the screen or be broader than the caller's scope. |
| API | `backend/app/api/v1/reconciliation_reports.py` | Filter validation, authorization, audit, response headers. |
| UI | `frontend/app/reconciliation/reports/page.tsx` | One page: report navigation, filters, KPI cards, tables, pagination, drill-down, export buttons. |

New dependency: `reportlab==4.2.5` (the repository had no PDF library). No database migration: every figure is derived
from existing tables. There is no cache and no background generation: each report is computed per request and per
user, and responses carry `Cache-Control: no-store`.

### Source tables

`bank_statement_transactions`, `treasury_transactions`, `bank_accounts`, `banks`, `legal_entities`, `groups`, `users`
(assignee names only), `reconciliation_runs`, `reconciliation_match_suggestions`, `reconciliation_match_groups` and
`..._members`, `reconciliation_match_decisions`, `reconciliation_accepted_claims`, `reconciliation_open_items`,
`reconciliation_open_item_events`, `audit_events` (write-only for the request record). See
`docs/RECONCILIATION_SCHEMA.md` for the schema.

## 2. Endpoints

| Method and path | Purpose |
|---|---|
| `GET /reconciliation/reports` | Catalog of reports (no data, so no entity scope applies). |
| `GET /reconciliation/reports/{report}` | A report. `{report}` is one of `overview`, `run-summary`, `matching-performance`, `exceptions`, `ageing`, `assignment`, `bank-accounts`, `entity-dashboard`, `group-dashboard`. |
| `GET /reconciliation/reports/{report}/export?format=xlsx\|pdf` | Export of every report except `overview`. Not paginated: it contains every authorized record matching the filters. |

Common query parameters: `date_from`, `date_to`, `as_of`, `legal_entity_id`, `bank_account_id`, `currency_code`,
`run_status`, `exception_status`, `category`, `assigned_to_user_id`, `unassigned`, `txn_date_from`, `txn_date_to`,
`min_age_days`, `max_age_days`, `page`, `page_size` (1–200). Invalid combinations return 422 (period inverted or longer
than 366 days, `as_of` in the future, inverted age or transaction-date ranges, `unassigned` with an assignee, a
non-3-letter currency). Default reporting period: the last 30 days ending today (UTC).

## 3. Counting rules (the same in every report)

* A **transaction**, a **proposal** (a Stage 5C suggestion or a Stage 5D group), an **accepted match** (a proposal with
  an explicit accept decision) and an **exception** (an open item) are four different things and are never added
  together. A `PENDING` proposal is never counted as a completed reconciliation.
* **Eligible transactions:** `ACTIVE` bank statement rows and `POSTED` ledger rows, in the entity, account and date
  window of the report. Reversed bank rows and non-posted ledger rows are excluded. Bank and ledger are different
  tables, so their counts may be added.
* **Each eligible transaction is in exactly one bucket**, so it is counted once however many proposals or groups
  mention it:
  * *accepted*: it has an accepted claim (bank rows: `MATCH`; ledger rows: `MATCH` or `TRANSFER`);
  * *proposed only*: not accepted, and covered by an **actionable** pending proposal, i.e. one the Stage 5E
    match-review queue can actually accept or reject: an unambiguous auto-tier suggestion (the Stage 5E predicate
    `_suggestion_pending_decidable`, reused rather than redefined) or a `PENDING` group;
  * *unmatched*: neither.

  `accepted + proposed + unmatched = eligible`, per side, always (asserted by tests).
* **Pending suggestions that are not actionable** (review tier, a competing-candidate tie, or an incomplete candidate)
  remain exceptions to investigate under Stage 5E. They are never described as awaiting a decision: their transactions
  are *unmatched*, and are reported separately as *under investigation* (a subset of unmatched, not a fourth bucket).
  The pending-proposal counts and the proposed-only buckets use this same definition, so they always agree.
* **Rejected** proposals stay rejected: they never count as accepted, and their transactions are unmatched.
* **Run-specific attribution (run summary).** A run's figures come from that run's own records: accepted = a claim
  created by one of the run's own decisions (linked through the decision's `reconciliation_run_id`); proposed = the
  run's own actionable pending suggestions and `PENDING` groups. A transaction accepted or proposed in a *different*
  run over the same account and an overlapping period is not inherited. The cross-run reports (matching performance,
  bank accounts, dashboards) deliberately classify each transaction once across all runs: accepted if an accepted
  claim exists anywhere, proposed if an actionable pending proposal exists anywhere.
* **Decision counts vs unique transaction counts** are shown side by side in the matching report and are **not**
  additive: one ledger row can be in an accepted one-to-many group and an accepted internal-transfer pairing. Only the
  de-duplicated totals may be summed. Per-type unique counts are not mutually exclusive.
* **Completion rate** = (accepted bank rows + accepted ledger rows) / (eligible bank rows + eligible ledger rows) × 100,
  half-up to 2 decimals; blank (not zero) when nothing is eligible. It uses unique transactions and accepted claims,
  never proposal counts. Entity and group rates are computed from summed counts, never averaged.
* **Exceptions** are open items. Account, entity and group counts use distinct `source_key`, so the same underlying
  exception seen by two runs counts once. The exceptions report lists items as stored.

## 4. Reports and KPI definitions

| Report | Main figures |
|---|---|
| Run summary | Per run (periods overlapping the reporting period): reference, entity, masked account, currency, period, created / executed dates, status, bank and ledger rows in scope, accepted / proposed / unmatched per side (attributed to that run only), pending and rejected proposals, pending suggestions under investigation (with the bank and ledger rows they leave unmatched), exceptions by status. Run rows are never summed. Pending proposals use the match-review predicate (an unambiguous auto-tier suggestion still `PENDING`, or a `PENDING` group). |
| Matching performance | Decision counts and unique transactions by type (one-to-one, one-to-many, many-to-one, batch, internal transfer, FX), pending and rejected proposals, the three-bucket table (with the "of which under investigation" subset of unmatched), and per-currency amounts (bank and ledger separately). |
| Open exceptions | Reference (`EXC-…`), entity, masked account, transaction reference and date, amount, currency, category, status, assignee, opened date, last investigation, age, notes indicator, resolution date. Default statuses: `OPEN`, `UNDER_REVIEW`. |
| Ageing | Outstanding items per band (0–7, 8–14, 15–30, 31–60, more than 60 days) with assigned / unassigned, amounts per currency, and ageing by account, entity and category. |
| Assignment | Assigned / unassigned outstanding items, items by team member and status, average resolution time, oldest outstanding items, outstanding items with no investigation activity, reopened items. |
| Bank accounts | Per account: entity, bank, masked number, currency, state, latest run, latest completed date, completed runs in period, eligible and accepted counts, outstanding / unassigned exceptions, oldest open exception, completion rate. |
| Entity / group dashboard | Accounts in scope, reconciled and not yet reconciled, eligible and accepted transactions, outstanding and unassigned exceptions, completion rate, ageing distribution; the group view adds a consolidated row and entity-by-entity comparison. |

**An account with no completed run overlapping the period is shown as NOT YET RECONCILED.** Its accepted, exception and
completion figures are blank, not zero. It still contributes its eligible transactions to entity and group completion
rates, so unreconciled work lowers the rate rather than being hidden.

**Reporting-period semantics.** Period-based reports (run summary, matching, bank accounts, dashboards) always apply the
period. The backlog reports (exceptions, ageing, assignment) apply it only when dates are supplied explicitly, and then
to each item's *transaction* date.

## 5. Ageing and history

* Timezone: **UTC**. *Age in days* = as-of date minus the UTC date the item was opened (`created_at`). The as-of date
  defaults to today and may not be in the future.
* **Status and assignee at the as-of date are rebuilt from the event history** (`reconciliation_open_item_events`,
  latest status event and latest assignment event before the end of that day), not inferred from the current status.
  An item closed today is therefore still outstanding "as of yesterday", and items created after the as-of date are
  excluded. Tests cover this directly.
* Outstanding means `OPEN` or `UNDER_REVIEW`. Resolution time = first `RESOLVED` event minus creation, averaged over
  items resolved at least once (shown with the number of items measured); undefined, not zero, when none.
* *Investigation activity* = investigation started, comment, evidence, resolution, closure, rejection or reopening; an
  assignment alone is not investigation.

## 6. Currency treatment

Every amount keeps its currency and is returned as an exact decimal **string**. Amounts in different currencies are
never added and nothing is converted: totals are listed per currency, and bank and ledger amounts are shown separately.
The persisted 0–2 decimal money policy is respected: the formatter refuses (rather than rounds) an amount with more
than two decimals. The only rounded figures are percentages and average durations (half-up, 2 dp), which are ratios,
not money. FX-matched groups are reported by their stored status and type only; no rate is recalculated.

## 7. Authorization and entity scope

* Viewing needs `BANK_RECONCILIATION:VIEW`. Exporting needs `BANK_RECONCILIATION:EXPORT` **and** VIEW: the export scope
  is the intersection of the two, so a download is never broader than what the user can see. `EXPORT` without `VIEW`
  reads nothing.
* The visible entities are derived on the server (`get_authorized_scope` → `resolve_scope_entity_ids`) and applied in
  SQL to every table the report touches. `legal_entity_id` and `bank_account_id` only *narrow* the result: an entity
  outside the scope returns 403, an account outside it returns 404 (indistinguishable from a nonexistent account, so
  ids cannot be enumerated).
* The group dashboard shows only authorized entities and says so; it never includes totals from unauthorized entities.
* Tests verify this for every report, for the exceptions drill-down, for exports in both formats, and by mutation:
  removing the scope filters or the export permission check makes the tests fail.

## 8. Exports

* **Excel:** `Report Info` (title, generation timestamp, level, reporting period, as-of date, timezone, entity and account
  scope, currency, applied filters), `KPIs`, one sheet per table, and `Definitions`. Amounts are written as numbers.
* **PDF:** landscape A4 with the same context block, KPIs, tables sized to their content, methodology, and a footer with
  the generation timestamp, level and page number.
* **Level label:** `ENTITY`, `MULTI-ENTITY (authorized entities only)` or `GROUP`.
* **Formula injection:** every text cell that begins with `=`, `+`, `-`, `@` or a control character (also after leading
  whitespace) is prefixed with an apostrophe, so no exported cell is ever a formula. Tests assert that no cell in an
  export has the formula type.
* Account numbers are masked (last four digits only) and internal ids are not exported. Filenames contain only the
  report name and period.
* Row cap: 50,000 rows per export; above that the request fails with 422 rather than silently truncating.

## 9. Audit

One `audit_events` record per request (`module` BANK_RECONCILIATION, `record_type` ReconciliationReport, `record_id` the
report name): `REPORT_GENERATED`, `REPORT_EXPORTED` and `REPORT_EXPORT_FAILED`. The payload holds the report, format,
applied filters, the derived entity scope, outcome and row count, and, for failures, the reason (e.g. `HTTP 403`). It
holds no transaction references or amounts. Export failures (including permission denials and the row cap) are
recorded. A failed or denied *view* is not audited. Existing audit and workflow history is never modified (tested).

## 10. Frontend

`/reconciliation/reports` (linked from `/reconciliation`): report buttons, filters shown only where they apply to the
selected report, KPI cards (definition and source on hover; cards with a drill-down open the exceptions report with the
filter applied), tables with server-side pagination, loading / empty / error states, a collapsible methodology section
and Excel / PDF export buttons. There are no charts. Entity choices come from the entity dashboard response and
account choices from the bank-accounts report, so they need only reconciliation access and are already limited to
what the user may see.

## 11. Known limitations

* **Items are per run.** The same underlying exception can appear once per run that saw it. Account, entity and
  dashboard counts de-duplicate by `source_key`; the exceptions list does not.
* **Unassignment does not exist** as a workflow action in Stage 5E, so there is no unassignment history to report.
* **Run summary computes each run separately** (a handful of queries per run on the visible page), and ageing and
  assignment aggregate in memory. Both are fine at normal volumes; a very large item backlog would warrant SQL-side
  aggregation.
* **Decision counts are not currency-filterable** (a decision belongs to a run, not a currency); the currency filter
  limits the accounts and transactions instead.
* **Unreconciled accounts** have blank exception figures by design (see section 4).
* **A proposal that conflicts with an acceptance in another run** (Stage 5E refuses to accept it) still counts as awaiting a
  decision in its own run until someone rejects it; its detail screen shows the conflict.
* **Cross-run reports classify per transaction, not per run**, by design; use the run summary for per-run attribution.
* **PDF tables use small type** to fit wide reports; the Excel export is the better format for analysis.
* **A denied or failed view is not audited**, only exports are.
* **Frontend lint** (`next lint`) is not configured in the repository, so only the production build and `tsc` were run.
* No external bank APIs, charts, scheduled reports or currency consolidation (all out of scope).

## 12. Test evidence

Run with the documented disposable test database (`docs/TEST_DATABASE_SAFETY.md`) on PostgreSQL 16.15:

* 92 tests in `tests/test_reconciliation_reports_{calculations,authorization,exports,audit_and_integrity}.py`
  (calculations 29, authorization 16, exports 35, audit and integrity 5) and
  `tests/test_reconciliation_reports_integrity_corrections.py` (7: run-specific attribution, pending-proposal semantics
  checked against the real Stage 5E match-review endpoints). Expected figures were derived by hand from a
  scenario built through the real Stage 5E API (accepted one-to-one, one-to-many and internal-transfer matches, a
  pending and a rejected proposal, a transaction in two accepted proposals, two currencies, a reversed row, an
  out-of-period row).
* The full suite (572 tests) passes. The existing Stage 5A–5E tests are unchanged.
* Mutation checks: disabling the formula-injection defence, the EXPORT permission check, the account scope, the
  open-item scope, or counting a pending proposal as accepted each caused tests to fail.
