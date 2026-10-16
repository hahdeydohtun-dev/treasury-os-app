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
| Per-action audit events; row-locked, concurrency-safe actions | Revoking an accept/reject decision (decisions are final) |
| **Explicit accept/reject of engine proposals** (one-to-one suggestions and Stage 5D groups), with a separate match-review queue | Choosing one candidate out of a tie |
| | Categories the engine cannot produce: DUPLICATE, BANK_CHARGE, FX_DIFFERENCE, MISSING_* (vocabulary only) |

## 2. Dispositions: proposal vs accepted match vs exception

Three different things must never be confused:

| Term | Meaning | Who/what sets it |
|---|---|---|
| **PENDING** (suggestion or group) | A **proposal awaiting a decision**. Not a reconciliation, however high its confidence. | The Stage 5C/5D engines (they never accept anything) |
| **ACCEPTED** | An **explicitly accepted** reconciliation match. | Only an authorized user, via the accept action (decision record written) |
| **REJECTED** | An **explicitly rejected** proposal, with a mandatory reason. Its transactions are no longer covered by it (they become exceptions unless another live proposal covers them). | Only an authorized user, via the reject action |
| **OPEN / UNDER_REVIEW** | An **unresolved exception** needing investigation | Open-item derivation (§3) |
| **RESOLVED / CLOSED** | Investigation outcome and closure - documentation, **never** an accounting posting | Workflow actions (§5) |

An earlier version of this stage treated an auto-tier `PENDING` suggestion and any `PENDING` group as if they were
"matched" and silently hid them. That was wrong: nothing in the codebase could ever set `ACCEPTED`, so the
"matched" state was an unverifiable assumption. It is corrected as follows.

### Where each engine output goes

| Engine output (ACTIVE bank / POSTED ledger rows only) | Queue | Item? |
|---|---|---|
| Unambiguous 1:1 suggestion in the auto-match tier (confidence ≥ Stage 5C `AUTO_MATCH_THRESHOLD`, not downgraded to `[review]`), `PENDING` | **Match review** (decision needed, counted) | No - it is a proposal, not an exception |
| `PENDING` Stage 5D group - ONE_TO_MANY, MANY_TO_ONE, BATCH, INTERNAL_TRANSFER or FX_MATCH | **Match review** (decision needed, counted) | No |
| Any of the above once **ACCEPTED** | Match review → "Accepted" | No (reconciled) |
| Any of the above once **REJECTED** | Match review → "Rejected" (disposition kept) | **Yes**: its transactions fall through to BANK_ONLY / LEDGER_ONLY, annotated with the rejection reason. Only transactions no other live proposal covers become exceptions - see "Internal-transfer rejection" below. |
| Unambiguous 1:1 suggestion, **review tier** (weak evidence) | Exceptions | `AMOUNT_VARIANCE` (item amount = the variance), else `TIMING_DIFFERENCE`, else `EXCEPTION` |
| 2+ tied 1:1 candidates | Exceptions | `AMBIGUOUS_MATCH` (one per bank row) |
| `AMBIGUOUS` Stage 5D groups | Exceptions | One item per anchor: `ONE_TO_MANY` / `MANY_TO_ONE` / `BATCH_PAYMENT` |
| No suggestion / no match | Exceptions | `BANK_ONLY` |
| POSTED ledger row in scope not covered by a live (`PENDING`/`ACCEPTED`) suggestion or group (any run) or an ambiguous group | Exceptions | `LEDGER_ONLY` (SQL `NOT EXISTS`) |
| `REVERSED` bank row | - | Never read |

Ties, `AMBIGUOUS` groups and review-tier suggestions are **not decidable** (accept/reject returns 400 with an
explanation pointing at the exceptions queue): a human must first investigate which candidate is right, which is
what an open item is for. Choosing one candidate out of a tie is not supported.

A `PENDING` proposal is *counted*, never hidden: the **Match review** queue shows the outstanding-decision count
(also on a tab on both queues), and `GET /reconciliation/match-proposals/summary` returns it.
The auto/review tier depends on the documented Stage 5C `[review]` reason prefix and confidence; Stage 5C is unchanged.

### Decisions

- `POST /reconciliation/match-proposals/{SUGGESTION|GROUP}/{id}/accept|reject`. Reject needs a reason (≥ 10 chars);
  accept's reason is optional.
- **Permission: `BANK_RECONCILIATION` `APPROVE`** - the repository's existing decision permission (already used for
  funding, facility and investment approvals). No new permission was invented. Viewing needs `VIEW`; a proposal the
  caller cannot view is reported as **404**.
- Each decision locks the proposal row, sets its status (`ACCEPTED`/`REJECTED`), writes an append-only
  `reconciliation_match_decisions` record (actor, time, previous/new status, reason, run, proposal) and one central
  `AuditEvent` (`MATCH_ACCEPTED`/`MATCH_REJECTED`). Unique constraints allow **one decision per proposal**.
- **Idempotent:** repeating the same decision returns `already_decided: true` and writes nothing. **Conflicting**
  decisions (accept then reject, or two concurrent requests) return **409**; exactly one is ever recorded. Decisions
  are final in this stage (no revoke).
- A rejection re-runs open-item derivation in the same transaction so the freed transactions become actionable
  immediately; existing items (including resolved/closed ones) are never touched.
- A decision changes only the proposal's status. It never touches a `TreasuryTransaction`, `BankStatementTransaction`,
  `BankBalance` or any cash record, and posts nothing.
- Rejecting releases the transactions from the engine's claim, per the existing Stage 5C/5D rule that only
  `PENDING`/`ACCEPTED` proposals claim a transaction. A *new* run may re-propose the same match.
- Acceptance claims are never released (decisions are final); see "Duplicate-acceptance protection".

### Duplicate-acceptance protection (cross-proposal, cross-run)

Locking a single proposal and recording one decision per proposal was **not enough**: two different proposals
can cover the same transaction, and both could be accepted. This was reproduced against the real engines
(re-running reconciliation after a second matching ledger row appears makes Stage 5C propose the same bank row
again in the new run; both acceptances returned 200) and is now prevented.

**Overlap scenarios, as found in the data model**

| Scenario | Classification | Behaviour |
|---|---|---|
| Suggestion + group claim the same bank/ledger row | **Conflicting** | Second acceptance → 409 |
| Two groups overlap on a bank or ledger row | **Conflicting** | Second acceptance → 409 |
| Two suggestions compete for one row, same run | **Ambiguous** (a tie) | Exceptions queue; not decidable (unchanged) |
| Two suggestions compete for one row, different runs (the natural re-run case) | **Competing proposals** | Both may sit PENDING; only one can be accepted, the other → 409 |
| Overlapping runs/periods propose the same row | Competing (as above) | As above |
| 1:1 suggestion + the INTERNAL_TRANSFER group that includes its leg | **Compatible by design** | Both may be accepted (separate `TRANSFER` namespace) |
| Two INTERNAL_TRANSFER groups share a leg | Conflicting | Second → 409 |
| Row accepted in run 1, then a new run executes | Reconciled | New run raises no false exception for it; a competing proposal → 409 |
| Reject one proposal while another still covers a row | Valid | Only rows no other live proposal covers become exceptions |

**Design.** Table `reconciliation_accepted_claims`: one row per transaction per **accepted** proposal, with partial
unique indexes `(bank_transaction, claim_kind)` and `(ledger_transaction, claim_kind)`. `claim_kind` is `MATCH` for
every bank-to-ledger proposal and `TRANSFER` for INTERNAL_TRANSFER groups. Accepting a proposal:

1. locks the proposal row; repeats of an already-recorded decision return idempotently;
2. checks **eligibility** of every member (bank row ACTIVE, ledger row POSTED, in the run's entity and bank account -
   a transfer's pair leg may be in another account of the same entity - with consistent currencies) → 409 if not;
3. checks for a **conflicting accepted claim in any run** → 409 with the conflicting decision/run;
4. inserts the decision **and its claims inside a savepoint, before writing anything else**; a unique violation is
   mapped to 409 and nothing else (decision, status change, audit event) is written.

**Why it is concurrency-safe.** Steps 2-3 only give a clear message. The guarantee is step 4: if two requests race
past the pre-check, the second `INSERT` into the claims table **waits** for the first transaction on the unique
index entry; when the first commits, the second fails with a unique violation, and when the first rolls back, the
second proceeds. This needs no application lock, works across workers/processes and across runs, and cannot be
bypassed by stale reads. Only `ACCEPTED` proposals claim, so two overlapping `PENDING` proposals can coexist and
accepting one **never** accepts or rejects the other; the loser stays `PENDING` (it can still be rejected) and its
detail endpoint reports `acceptance_conflict`. Rejections create no claims.

**Verified how.** A deterministic test opens two real PostgreSQL connections with the application pre-check
disabled: session A accepts without committing, session B's acceptance of an overlapping proposal is shown to
*block*, then fails with 409 once A commits, leaving one decision, one audit event and the loser `PENDING`. A
mutation check (dropping the unique indexes) makes that test fail, confirming it depends on the database
guarantee. The HTTP-level race tests (`asyncio.gather`) prove the end-to-end result (one 200, one 409) but in the
ASGI harness the pre-check normally wins the race first, so they are *not* independent proof of the index.

**Migration.** `d5e508d02a28` creates the table and indexes and backfills claims from decisions already `ACCEPTED`.
Because earlier decisions were never checked for overlap, a **pre-flight** refuses to migrate (changing nothing) if
existing accepted decisions already conflict, listing them; an operator must decide which stands.

### Internal-transfer rejection (traced)

Rejecting an `INTERNAL_TRANSFER` group frees its two ledger legs from the group's claim, and derivation then
treats each leg independently. What happens to a leg depends on its actual coverage (all verified by tests):

- **Leg covered by another live proposal** (typically the primary leg's one-to-one suggestion): not an exception;
  it stays represented by that proposal in the match-review queue.
- **Leg in this run's account scope, covered by nothing**: becomes an actionable `LEDGER_ONLY` exception carrying
  the rejection reason.
- **Leg in another account**: it is outside this run's scope, so this run raises nothing. While the transfer
  proposal is `PENDING` the other account's run does not call it unmatched (it is covered by the proposal); once
  rejected, that account's run surfaces it as `LEDGER_ONLY` the next time it is synchronized. It is therefore not
  silently lost, but it appears in the *other account's* run, not the rejecting run.

The rejection decision and reason stay in the decision record and audit trail in every case.

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
| `GET /reconciliation/match-proposals` | Match-review queue. Default `status=PENDING`; `ACCEPTED`/`REJECTED` for history; filters `kind`, `relationship_type`, `run_id`, `legal_entity_id` |
| `GET /reconciliation/match-proposals/summary` | `{pending_decisions, pending_suggestions, pending_groups, open_exceptions}` |
| `GET /reconciliation/match-proposals/{kind}/{id}` | Supporting bank/ledger transactions, group aggregates, FX provenance, decision, and `acceptance_conflict` (why accepting would fail) |
| `POST /reconciliation/match-proposals/{kind}/{id}/accept` · `/reject` | Explicit decision (see above) |

Age is computed at read time from `item_date` (bank transaction date or ledger event date): up to today while
actionable, up to the resolution/closure/rejection date afterwards. It is never stored.

## 8. Data model

`reconciliation_open_items` (Stage 5B) gained: `source_key`, `item_date`, `priority`, `due_date`,
`assigned_by_user_id`, `assigned_at`, `resolved_by_user_id`, `resolution_reason`, `closed_at`,
`closed_by_user_id`, `rejected_at`, `rejection_reason`, plus `unique(run, source_key)`. The existing
`description`/`reason` keep their meaning (the engine's classification text). New table
`reconciliation_open_item_events`; new enum value `AMBIGUOUS_MATCH`; and `reconciliation_match_decisions` (migration `f4c533db287d`) and `reconciliation_accepted_claims` (migration `d5e508d02a28`) - tables only, no existing column or status changed. Migration `23ffdb804f43`: new NOT NULL
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
- Resolving/rejecting an *open item* does not change any proposal's status; accepting/rejecting a *proposal* is the
  separate match-review action. Decisions are final (no revoke) and one candidate cannot be chosen out of a tie.
- A later run can re-propose a match that was rejected; no memory of rejections is fed back to the engines (that
  would be adaptive matching, Stage 5G).
- No bulk assignment, exports, notifications, or SLA escalation.
- The queue page shows assignee names only for users returned by the assignable-users list.
