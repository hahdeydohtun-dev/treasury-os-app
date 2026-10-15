# Stage 5E — Reconciliation Open-Item Workflow

Turns a completed reconciliation run (Stages 5A–5D, unchanged) into an auditable work queue:
**execute → outstanding items derived automatically → assign → investigate → resolve → close,
with a complete history.**

## 1. Implemented vs deferred

| Implemented | Deferred (not built) |
|---|---|
| Deterministic, idempotent open-item derivation from a run | Reconciliation reports (5F) |
| Assignment/reassignment with history, due date, priority | Adaptive/AI matching (5G) |
| State machine: start, resolve, close, reject, reopen | Full reconciliation frontend (5H) |
| Append-only investigation history (comments, evidence **references**) | Evidence **file upload** (no secure file store exists) |
| Work-queue API with filters/sorting; focused queue page | Bulk assignment; exports |
| Per-action audit events; row-locked, concurrency-safe actions | Accepting/rejecting engine *suggestions* (items never change them) |
| | Categories the engine cannot produce: DUPLICATE, BANK_CHARGE, FX_DIFFERENCE, MISSING_* (vocabulary only) |

## 2. What becomes an open item (and what does not)

An open item is an **unresolved exception**, never "every suggestion". Stage 5C only ever persists
`PENDING` suggestions (nothing auto-accepts), so "matched" is derived from the engine's own tiering —
no `PENDING` suggestion is assumed to be a reconciliation.

| Situation (per bank row / ledger row, ACTIVE / POSTED only) | Result |
|---|---|
| Unambiguous 1:1 suggestion, **auto-match tier** (confidence ≥ Stage 5C `AUTO_MATCH_THRESHOLD`, not downgraded to `[review]`) or `ACCEPTED` | **No item** (matched) |
| Member of a live (`PENDING`/`ACCEPTED`) Stage 5D group (one-to-many, many-to-one, batch, internal transfer, FX) | **No item** |
| Unambiguous 1:1 suggestion, **review tier** | `AMOUNT_VARIANCE` (amounts differ; item amount = the variance), else `TIMING_DIFFERENCE` (dates differ), else `EXCEPTION` (weak evidence) |
| 2+ tied 1:1 candidates | `AMBIGUOUS_MATCH` (one per bank row; the tied ledger rows add nothing) |
| `AMBIGUOUS` Stage 5D groups | One item per anchor: `ONE_TO_MANY` / `MANY_TO_ONE` / `BATCH_PAYMENT`, however many competing groups share it |
| No suggestion / no match | `BANK_ONLY` |
| `REVERSED` bank row | **Never read** |
| POSTED ledger row in scope, not claimed by any live suggestion or group (any run), not in an ambiguous group | `LEDGER_ONLY` (decided in SQL with `NOT EXISTS`, never by loading the ledger) |

Note: the high-value "review" downgrade exists only as the `[review]` reason prefix in Stage 5C output;
Stage 5E reads that documented prefix and leaves Stage 5C untouched.

Items are **per run** (`unique(run, source_key)`). Re-running reconciliation creates a new run with its
own items; no cross-run de-duplication is attempted.

## 3. Idempotency and atomicity

- `source_key = CATEGORY:bank_id:ledger_id`, unique per run; bulk insert uses `ON CONFLICT DO NOTHING`.
- Re-synchronizing creates no items, no `CREATED` events and no audit event (an audit event is written only
  when something was created).
- Sync runs inside a **savepoint**: any failure leaves no partial items/events (tested by forcing a failure
  after the inserts).
- Sync is invoked automatically at the end of `execute_reconciliation_run` (same transaction — the one
  integration point into earlier stages) and is available as `POST /runs/{id}/open-items/sync` for
  retry/backfill on a `COMPLETED` run.
- Measured cost: the 10,000-ledger/1,000-bank scale run now also derives ~11k items; execute went from
  ~20 s to ~37 s on the dev container (informational, not a benchmark).

## 4. State machine

```
OPEN ──start──▶ UNDER_REVIEW ──resolve──▶ RESOLVED ──close──▶ CLOSED
  │                  │                       │                   │
  └──────reject──────┴──▶ REJECTED           └──────reopen───────┴──▶ OPEN   (also REJECTED ─reopen▶ OPEN)
```

- Resolution (reason ≥ 10 chars) and closure are separate actions; resolve only from `UNDER_REVIEW`.
- Reject (reason ≥ 10 chars) from `OPEN`/`UNDER_REVIEW`; rejected items stay in history.
- Reopen (reason ≥ 10 chars) from `RESOLVED`/`CLOSED`/`REJECTED` — clears the *current* disposition
  summary fields; the earlier resolution stays in the event history.
- `CLOSED` and `REJECTED` are terminal for ordinary actions (no assign, no start, no resolve); only
  reopen leaves them. Comments are still allowed so follow-up notes can be recorded. Closing never deletes.
- Assignment only while `OPEN`/`UNDER_REVIEW`; the assignee must be an **active** user holding
  `INVESTIGATE` on the item's entity; assigning to the current assignee is refused.

## 5. Authorization (existing `BANK_RECONCILIATION` permissions)

| Action | Permission |
|---|---|
| list / detail / history | `VIEW` |
| assign, assignable-users | `ASSIGN` |
| start investigation | `INVESTIGATE` |
| comment, add evidence reference | `COMMENT` |
| resolve | `RESOLVE` |
| close, reject, reopen | `CLOSE` |
| synchronize a run | `EXECUTE` |

Entity/group scoping is applied in SQL for lists. An item the caller cannot `VIEW` returns **404**, identical
to a non-existent id, so another entity's item ids cannot be enumerated. No separate segregation-of-duties
or approval rule was added, because the repository has no such policy for reconciliation.

## 6. Audit

Each action writes (a) one row to `reconciliation_open_item_events` — the durable, append-only
investigation history (actor, from/to status, assignee, note, evidence reference, timestamp) — and (b) one
central `AuditEvent` (`ASSIGNED`/`REASSIGNED`, `INVESTIGATION_STARTED`, `COMMENT_ADDED`, `EVIDENCE_ADDED`,
`RESOLVED`, `CLOSED`, `REJECTED`, `REOPENED`, plus `SYNC_OPEN_ITEMS` on the run) with previous/new state and
reason. No endpoint updates or deletes history. Evidence is a plain-text reference (document/journal/ticket
number); URLs and file paths (including `C:\...`) are refused.

## 7. API

| Method & path | Purpose |
|---|---|
| `GET /reconciliation/open-items` | Work queue. Default = `OPEN`+`UNDER_REVIEW`. Filters: `legal_entity_id`, `bank_account_id`, `run_id`, `category`(×), `status`(×), `priority`, `assigned_to_user_id`, `unassigned`, `min/max_age_days`, `min/max_amount`, `currency_code`; `sort=oldest|amount_desc|priority`; `limit`/`offset`; `X-Total-Count` header |
| `GET /reconciliation/open-items/{id}` | Detail: source transactions, engine suggestions, match groups |
| `GET /reconciliation/open-items/{id}/history` | Chronological history |
| `POST /reconciliation/open-items/{id}/assign` | `{assignee_user_id, reason, due_date?, priority?}` |
| `POST .../start-investigation` · `.../comments` · `.../evidence` · `.../resolve` · `.../close` · `.../reject` · `.../reopen` | Workflow actions |
| `GET /reconciliation/open-items/assignable-users?legal_entity_id=` | Active users who can work the entity's items |
| `POST /reconciliation/runs/{id}/open-items/sync` | Idempotent (re)synchronization of a COMPLETED run |
| `GET /reconciliation/runs/{id}/open-items` | (Stage 5B) items of one run |

Age is computed at read time from `item_date` (bank transaction date or ledger event date): up to today while
actionable, up to the resolution/closure/rejection date afterwards. It is never stored.

## 8. Data model

`reconciliation_open_items` (Stage 5B) gained: `source_key`, `item_date`, `priority`, `due_date`,
`assigned_by_user_id`, `assigned_at`, `resolved_by_user_id`, `resolution_reason`, `closed_at`,
`closed_by_user_id`, `rejected_at`, `rejection_reason`, plus `unique(run, source_key)`. The existing
`description`/`reason` keep their meaning (the engine's classification text). New table
`reconciliation_open_item_events`; new enum value `AMBIGUOUS_MATCH`. Migration `23ffdb804f43`: new NOT NULL
columns are added nullable, backfilled, then tightened; downgrade drops the Stage 5E workflow
tables/columns only (operational workflow data; no financial value is touched) and cannot remove the enum
value. One Stage 5B test constructs raw open-item rows and was updated to supply the two now-mandatory fields.

## 9. Financial integrity

No workflow action touches `TreasuryTransaction`, `BankStatementTransaction`, `BankBalance`, cash,
investments or facilities; resolution means *investigated and documented*, never *posted*. A test snapshots
all ledger, statement and balance rows, runs every action (including reopen/reject), and asserts nothing
changed. Persisted-money precision (0–2 decimals) is unchanged; item amounts are `Numeric(20,2)`.

## 10. Known limitations

- Per-run items: the same underlying exception appears once per run that observes it.
- "Matched" for 1:1 suggestions relies on the Stage 5C auto/review tier (confidence + `[review]` prefix).
- Resolving/rejecting an item does not change the engine's suggestion/group status (no accept/reject of
  suggestions in this stage).
- No bulk assignment, exports, notifications, or SLA escalation.
- The queue page shows assignee names only for users returned by the assignable-users list.
