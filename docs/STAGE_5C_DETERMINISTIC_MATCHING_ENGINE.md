# Stage 5C — Deterministic Bank Reconciliation Matching Engine

## 1. Objective

Determine, deterministically, which `BankStatementTransaction` rows
(Stage 5A evidence) plausibly correspond to which `TreasuryTransaction`
rows (the single internal cash ledger), and persist that assessment as
`ReconciliationMatchSuggestion` rows. Stage 5C does not decide anything
final: a suggestion is the deterministic engine's assessment, never a
human-approved reconciliation, and never a financial action.

## 2. Architecture

```
BankStatementTransaction  +  TreasuryTransaction
              |
              v
   reconciliation_candidate_generation.py   (DB-side filtering only)
              |
              v
   reconciliation_scoring.py                (pure functions, no I/O)
              |
              v
   reconciliation_matching_engine.py         (orchestration + persistence)
              |
              v
     ReconciliationMatchSuggestion
```

Deliberately layered per the spec's own candidate-generation/scoring/
selection separation: candidate generation never scores anything,
scoring never queries the database, and only the orchestration layer
persists. This separation is required groundwork for Stage 5D's
advanced matching (one-to-many, batch payments, etc.), which will reuse
the same candidate-generation and scoring building blocks without
needing to touch them.

## 3. Inputs

- `BankStatementTransaction` (Stage 5A evidence — read-only; only
  `status == ACTIVE` rows are eligible — see Section 3a).
- `TreasuryTransaction` (the single internal cash ledger — read-only;
  only `status == POSTED` rows are eligible).
- The run's own `ReconciliationConfiguration` (resolved and stored at
  run *creation* time in Stage 5B — Stage 5C never re-resolves a newer
  version at execution time).

## 3a. Bank evidence eligibility (final integrity hardening)

Only `BankStatementTransaction` rows with `status == ACTIVE` are ever
counted, matched, or offered as evidence. A `REVERSED` bank statement
transaction is invisible to Stage 5C entirely:

- Excluded from `execute_reconciliation_run`'s own counting query, so
  it never inflates `statement_transaction_count`/
  `eligible_transaction_count`.
- Excluded from `run_matching_for_run`'s bank-transaction selection
  query, so it never reaches `run_matching_for_bank_transaction`, never
  generates a candidate, and never generates a suggestion.

Both queries apply the identical `BankStatementTransactionStatus.ACTIVE`
filter — a single documented definition of "eligible bank statement
transaction," not two independently-maintained ones.
`statement_transaction_count` and `eligible_transaction_count` remain
numerically identical in Stage 5C because this stage defines no OTHER
bank-side eligibility rule beyond ACTIVE status; a future stage that
introduces additional bank-side exclusions (e.g. a transaction already
consumed by a finalized reconciliation) would be the point at which the
two counts could diverge. Verified by
`test_active_transaction_can_participate_reversed_cannot`,
`test_reversed_transaction_creates_zero_suggestions`,
`test_reversed_transaction_does_not_inflate_run_counts`, and
`test_reversed_transaction_cannot_reach_matching_via_direct_call` (the
last of which calls `run_matching_for_run` directly, not only through
the top-level API, to prove the filter is enforced at the actual query,
not merely incidentally by some other layer).

## 4. Candidate generation

`reconciliation_candidate_generation.py::generate_candidates` issues
exactly one indexed SQL query per bank statement transaction, filtering
on: `legal_entity_id` (exact), `bank_account_id` (exact),
`transaction_currency_code` (exact), `direction` (only the compatible
one — see Section 6), `status = POSTED`, an `event_date` window (the
bank transaction's own date ± the run's configured
`date_tolerance_days`), and a `transaction_amount` band (± the run's
configured `amount_tolerance_pct`). Rows already claimed by a live
(`PENDING`/`ACCEPTED`) suggestion **from the same legal entity** are
excluded (`already_claimed_treasury_transaction_ids`). No candidate
outside these filters is ever loaded into Python — verified by
`test_candidate_generation_uses_database_filtering_not_full_scan`
(51 rows seeded, only the 1 genuinely in-scope row returned).

**Entity-scoped claim lookup (final hardening patch)**:
`already_claimed_treasury_transaction_ids` accepted a `legal_entity_id`
parameter from its first implementation, but the query itself did not
use it — an oversight found in a later hardening pass, not a redesign.
`ReconciliationMatchSuggestion` carries no `legal_entity_id` column of
its own, so the fix joins to `TreasuryTransaction` (via
`treasury_transaction_id`) and filters on
`TreasuryTransaction.legal_entity_id` — the one authoritative existing
relationship that already carries entity ownership, rather than adding
a new denormalized column or inventing a different relationship. This
is a defense-in-depth/correctness fix, not a response to a demonstrated
cross-entity data leak: `TreasuryTransaction` UUIDs are globally unique,
so the unscoped version never actually returned another entity's real
transaction as a false positive — but an unscoped global scan across
every entity's claims was still broader than the function's own
contract implied, and a genuine correctness risk for future callers.
Verified by `test_already_claimed_lookup_is_entity_scoped`,
`test_entity_filter_comes_from_treasury_transaction_relationship_not_uuid_assumption`,
`test_candidate_generation_regression_entity_b_never_a_candidate_for_entity_a_run`,
and `test_cross_group_claimed_transactions_never_leak`. No migration was
required — `TreasuryTransaction.legal_entity_id` was already indexed.

## 5. Matching signals and normalization

Deterministic only — no AI, no embeddings, no network calls
(`reconciliation_normalization.py`):

- **Reference**: `normalize_reference` — uppercase + trimmed + collapsed
  whitespace. Deliberately does NOT strip separators — `"ABC-123"` and
  `"ABC123"` are never treated as equal.
- **Narration**: `normalize_narration` — lowercase + trimmed + collapsed
  whitespace + a small explicit set of cosmetic punctuation removed,
  then tokenized (`tokenize_narration`) for deterministic overlap
  comparison (a token-set intersection ratio, never fuzzy/edit-distance
  matching).
- **Direction**: the bank statement's own `DEBIT`/`CREDIT` vocabulary
  (kept distinct from `TreasuryTransaction`'s `INFLOW`/`OUTFLOW`) —
  `CREDIT` is compatible only with `INFLOW`, `DEBIT` only with
  `OUTFLOW`. Incompatible direction is an outright candidate
  **rejection**, enforced at the SQL level (Section 4), never merely a
  low score.

## 6. Scoring model

`reconciliation_scoring.py::score_candidate` — the single authoritative
scoring function. Fixed-point buckets, not a continuous blend (see the
module's own docstring for why this deviates from the old bank-matcher
application's continuous percentage-weighted model — summarized: only a
discrete-bucket model can cleanly express Section 21's EXACT-vs-
TOLERANCE distinction):

| Signal | Exact | Tolerance/partial | None |
|---|---|---|---|
| Amount | 40 | 20 | — |
| Reference | 30 (exact normalized match) | 15 (deterministic substring overlap) | 0 |
| Date | 15 | 5 | — |
| Direction | 10 (compatible) | — | candidate rejected |
| Narration | 5 (≥60% token overlap) | — | 0 |

Maximum score: 100. Eligibility for amount/date (whether a candidate is
even considered at all) is enforced by candidate generation's own SQL
filters (Section 4) — by the time a candidate reaches scoring, it has
already passed the tolerance gate; scoring only distinguishes *exact*
(zero difference) from *within-tolerance* for point allocation.

## 7. Thresholds

Centralized in `reconciliation_scoring.py`, never scattered:
`AUTO_MATCH_THRESHOLD = 70`, `REVIEW_THRESHOLD = 40`. A candidate
scoring below `REVIEW_THRESHOLD` is not treated as a plausible match at
all (excluded before ranking). A candidate at or above
`AUTO_MATCH_THRESHOLD`, unambiguous, is framed as an "auto-match"
candidate in its `reason` text; between the two thresholds, "review."

**High-value conservative gating** (Section 41): when the bank
transaction's amount exceeds the run's configured
`high_value_threshold` and the winning candidate has zero reference
points (i.e. the score is driven by amount+date+narration alone, the
least reliable combination), the suggestion is downgraded from
"auto-match" to "review" framing even if the raw score clears
`AUTO_MATCH_THRESHOLD` — verified by
`test_high_value_transaction_with_no_reference_is_not_labeled_auto_match`
and its counterpart proving a genuine reference match is not downgraded.

## 8. Confidence

Stored as the raw 0-100 point total (not a 0-1 probability) on
`ReconciliationMatchSuggestion.confidence`. Confidence is never an
approval and never a final reconciliation decision — it is the
deterministic engine's own assessment, always reproducible from the
same inputs.

## 9. Match types

`MatchType` extended with `EXACT`/`TOLERANCE` (Section 21) —
`CandidateScore.is_exact_match` is true only when amount, date, AND
reference are *all* exact; any one of them being merely
within-tolerance makes the whole suggestion `TOLERANCE`. The remaining
`MatchType` values (`ONE_TO_MANY`, `MANY_TO_ONE`, `BATCH_PAYMENT`,
`INTERNAL_TRANSFER`, `BANK_CHARGE`, `FX_ADJUSTED`, `ONE_TO_ONE`, `OTHER`)
are reserved for Stage 5D — Stage 5C is one-to-one matching only and
never sets any of them.

## 10. Rule version

`MATCHING_RULE_VERSION = "5C-1.0"` (`reconciliation_scoring.py`) —
stored on every suggestion AND on the run itself
(`ReconciliationRun.matching_rule_version`). Identifies the scoring
model, candidate rules, normalization rules, and thresholds together. A
future rule change must introduce a new version string; this codebase
never silently redefines what an existing version means.

## 11. Configuration version

Stage 5B's own guarantee is unchanged and reused as-is: a run's
`configuration_id` was resolved at *creation* time and never
re-resolved at execution — verified by
`test_run_uses_exact_stored_configuration_not_a_later_version` (a
second, looser configuration created after the run has zero effect on
its execution).

## 12. Candidate selection and tie handling

Deterministic ranking: `(score DESC, ledger.event_date ASC,
str(ledger.id) ASC)` — fully explicit, never dependent on database row
order or dict iteration. If exactly one candidate clears the top score,
it becomes the single suggestion. **If two or more candidates tie for
the top score, the engine does NOT arbitrarily pick one** — every tied
candidate is persisted as its own `PENDING` suggestion, each reason
explicitly stating "AMBIGUOUS: N candidates tied at score X," and
`ReconciliationRun.ambiguous_count` is incremented once for that bank
transaction — verified by
`test_ambiguous_tied_candidates_both_persisted_never_arbitrarily_chosen`.

## 13. Idempotency

Two layers:

1. **Application-level**, checked before every insert:
   `_existing_suggestion` (exact triple match) and, more importantly,
   `_bank_transaction_already_processed` (any suggestion at all for
   this run + bank transaction).
2. **Database-level**: two partial unique indexes on
   `reconciliation_match_suggestions` —
   `ux_reconciliation_match_suggestions_identity`
   `(run, bank_txn, treasury_txn) WHERE treasury_txn IS NOT NULL`, and
   `ux_reconciliation_match_suggestions_no_candidate`
   `(run, bank_txn) WHERE treasury_txn IS NULL`.

**A genuine bug was found and fixed during testing**: the first
implementation only checked idempotency against the *specific*
candidate a repeated pass happened to land on. Because a treasury
transaction claimed by pass 1's suggestion becomes excluded from
candidate generation on pass 2 (Section 39's own "already claimed"
rule), pass 2 would conclude "no candidate found" and insert a
*second, different* (null-treasury) suggestion for the same bank
transaction — not a duplicate of the first, but a spurious extra one.
Fixed by adding `_bank_transaction_already_processed` as an early guard,
checked *before* any candidate generation, so a repeated pass over an
already-processed bank transaction is a true no-op regardless of how
the candidate population may have shifted. Verified by
`test_repeated_execution_does_not_duplicate_suggestions`.

## 14. Concurrency

Two independent protections, addressing two different races:

**Same run, concurrent execution requests**: Stage 5B's row-locked
execution boundary (`load_run_for_update`) — a genuine concurrent-request
test (`asyncio.gather`) proves exactly one of two simultaneous
executions of the SAME run succeeds and exactly one suggestion set is
ever persisted
(`test_concurrent_run_execution_cannot_execute_twice_or_duplicate_suggestions`).

**Different runs, same reconciliation scope (final integrity
hardening)**: the run's own row lock cannot protect against this, since
two different `ReconciliationRun` rows never contend for the same row.
`acquire_reconciliation_scope_lock`
(`reconciliation_service.py`) takes a PostgreSQL transaction-scoped
advisory lock (`pg_advisory_xact_lock`), keyed deterministically off
`(legal_entity_id, bank_account_id, period_start, period_end)` via
`hashtextextended` — database-backed, survives multiple workers/
processes, requires no explicit release (auto-released on COMMIT or
ROLLBACK, so a failed run's lock is never leaked). Acquired as the very
first action inside `execute_reconciliation_run`'s own transaction,
before any counting, candidate generation, or suggestion persistence,
and held for the remainder of that transaction. A second concurrent
request for the identical scope blocks until the first transaction
ends, then proceeds against the now-updated claimed-transaction set —
so the same `TreasuryTransaction` is never consumed by two competing
runs, verified by
`test_different_runs_same_scope_cannot_match_concurrently`. Scoped
narrowly (never a global lock): two genuinely independent scopes (e.g.
different bank accounts) execute fully concurrently, verified by
`test_different_scopes_execute_concurrently_without_unnecessary_serialization`.

Only these two guarantees are made — Stage 5C does not claim any
stronger concurrency guarantee (e.g. serializing all reconciliation
activity for an entire entity) than what is described above.

## 15. Entity isolation

Every candidate query includes `legal_entity_id` as a hard, non-optional
predicate — verified by
`test_entity_a_bank_transaction_cannot_match_entity_b_ledger_transaction`
(identical amount/reference/date across two entities produces zero
candidates, not merely a low score).

## 16. Currency isolation

Every candidate query includes `transaction_currency_code` as a hard
predicate — verified by `test_cross_currency_transaction_rejected`
(identical amount across USD/NGN produces zero candidates). Stage 5C
performs no FX conversion whatsoever.

## 17. Account isolation

Every candidate query includes `bank_account_id` as a hard predicate,
distinct from entity isolation — verified by
`test_wrong_bank_account_candidate_rejected` (same entity, different
account, zero candidates).

## 18. Financial side-effect prohibition

`run_matching_for_run`/`run_matching_for_bank_transaction` only ever
`SELECT` from `TreasuryTransaction`/`BankStatementTransaction` and
`INSERT` into `ReconciliationMatchSuggestion` — verified explicitly by
`test_matching_execution_never_modifies_treasury_transaction_or_bank_balance`
(a `TreasuryTransaction`'s own amount/status and a `BankBalance`'s own
closing balance are read before and after execution and found
byte-for-byte identical; no new `TreasuryTransaction` row is created
either).

## 19. API

No new endpoints — `POST /reconciliation/runs/{id}/execute` (Stage 5B)
now actually performs matching. `ReconciliationRunOut` gained
`candidate_count`/`suggestion_count`/`matched_count`/`ambiguous_count`/
`unmatched_count`/`matching_rule_version`.
`GET /reconciliation/runs/{id}/suggestions` (already existed, always
empty in Stage 5B) now returns real suggestions.

## 20. Tests

Test counts stated here are the actual `pytest --collect-only -q`
result at the time of writing, not a carried-forward estimate:
`tests/test_reconciliation_matching_engine.py` collects **35 tests**
(basic signals, currency/entity/account isolation, period/date
tolerance, tie handling, determinism, persistence, idempotency,
concurrency — both same-run and cross-run scope — configuration
versioning, financial integrity, failure handling, high-value gating,
normalization units, candidate-generation performance, entity-scoped
claim protection, and ACTIVE-only bank evidence); the Stage 5B suite
(`tests/test_reconciliation_data_model.py`) collects **26 tests**,
unchanged. The full repository's own `pytest --collect-only -q`
collects the whole Stage 0-5C suite together (see the Stage 5C final
integrity hardening report for the exact total at that point in time —
this document does not restate the whole-repository count, since it
changes with every stage and hardening pass; run the command yourself
for the current figure).

## 21. Performance/indexing

No new indexes were required beyond what Stage 5A/5B already created
(`BankStatementTransaction`/`TreasuryTransaction` are already indexed on
`legal_entity_id`, `bank_account_id`, `transaction_date`/`event_date`,
`currency_code`). `test_candidate_generation_uses_database_filtering_not_full_scan`
demonstrates the SQL predicates themselves narrow a 51-row population to
exactly 1 in-scope row — candidate generation never loads an unbounded
population into Python.

## 22. Limitations

- One-to-one matching only — see Section 23 explicitly.
- Narration matching is deterministic token-overlap, not semantic
  understanding — two narrations describing the same real-world event
  in very different words will not match on narration alone (this is
  intentional: Section 14 explicitly forbids AI/embeddings here).
- The candidate window is symmetric (± tolerance) on both amount and
  date; no asymmetric "bank always later" convention is modeled yet.

## 23. Explicit Stage 5D+ deferrals

Stage 5C is deterministic ONE-TO-ONE matching only. The following are
explicitly **NOT** implemented:

- AI matching is not implemented.
- Adaptive learning is not implemented.
- One-to-many and many-to-one matching are not implemented.
- Batch matching is not implemented.
- Internal-transfer matching is not implemented.
- FX matching is not implemented.
- Open-item workflow is not implemented (Stage 5E).
- Reconciliation reporting is not implemented (Stage 5F).
