# Stage 5D — Advanced Bank Reconciliation Matching

## 1. Purpose

Extend Stage 5C's deterministic one-to-one matching engine (unchanged,
frozen) with bounded, deterministic ADVANCED matching: one-to-many,
many-to-one, batch payments, internal transfers, and evidence-based FX
matching. Stage 5D never redesigns Stage 5C's scoring, thresholds,
candidate-generation rules, or rule version (`5C-1.0` is untouched);
Stage 5D introduces its own independently versioned rule set
(`5D-1.0`).

## 2. Architecture

```
Stage 5C one-to-one pass (unchanged)
              |
              v
   Stage 5D advanced-matching pass, SAME execution transaction
   (reconciliation_advanced_matching.py::run_advanced_matching_for_run)
              |
    +---------+---------+---------+
    |         |         |         |
one-to-many  many-to-  internal  FX
             one       transfer  matching
    |         |         |         |
    +---------+---------+---------+
              |
              v
     ReconciliationMatchGroup + ReconciliationMatchGroupMember
```

Layered per Stage 5C's own precedent: candidate generation (reference-
driven, bounded), grouping/scoring (`_find_summing_grouping`,
`select_fx_rate`), and persistence (`_persist_group`) stay separate
functions, never one giant routine.

## 3. One-to-many / many-to-one

Grouping is driven entirely by **normalized reference evidence**
(reusing Stage 5C's own `normalize_reference` unchanged) — never amount
alone. For a bank transaction Stage 5C left unmatched,
`_reference_matched_ledger_candidates` gathers TreasuryTransaction rows
sharing its normalized reference, within the same entity/account/
currency/compatible-direction/date-window Stage 5C itself would use, and
not already claimed. `_find_summing_grouping` then checks whether the
candidates' aggregate amount matches the bank amount within the run's
own configured tolerance. Many-to-one is the exact symmetric case,
starting from an unclaimed ledger transaction and gathering same-
reference bank transactions.

**Reference narrowing happens in SQL, before any `LIMIT`.** An earlier
version applied `LIMIT` first and filtered by reference in Python; with
thousands of same-date ledger rows that silently dropped the real
candidates (found by the 10,000-row scale test). Reference matching now
uses a SQL mirror of `normalize_reference` (`_normalized_sql`), results
are ordered deterministically (`event_date, id`) and then bounded by
`max_candidates_evaluated`, with a Python re-check as defence in depth.
Many-to-one first derives the set of references occurring on 2+ ACTIVE
in-scope bank rows (the only ones that can form a group) and narrows the
ledger query to those, instead of loading every unclaimed ledger row.

**Bounded search (no combinatorial explosion)**: the full candidate set
is tried first; if it doesn't sum within tolerance, each "drop exactly
one member" subset is tried (O(N), never `itertools.combinations` over
arbitrary subsets). If exactly one drop-one subset matches, it is used.
If two or more equally valid drop-one subsets exist, this is genuine,
bounded ambiguity — every competing subset is persisted as its own
`AMBIGUOUS`-status group, and none is arbitrarily chosen.

## 4. Batch payments

`BankStatementTransaction` has no dedicated batch-reference field (only
`bank_reference`/`external_transaction_id`/`narration` — inspected in
the actual schema, not assumed). Rather than invent one, `BATCH` is
distinguished from a plain `ONE_TO_MANY`/`MANY_TO_ONE` grouping by an
explicit "BATCH" keyword (case-insensitive) in the bank transaction's
own reference or narration — genuine, deterministic evidence, never an
amount-only inference. An amount aggregate that coincidentally matches,
with no shared reference and no batch keyword at all, never forms any
group (verified explicitly).

## 5. Internal transfers

Reuses the existing `TreasuryTransaction.transfer_pair_id` field
unchanged (both legs of a transfer share the same value — no new
transfer ledger, no mutation of `TreasuryTransaction`). Only considers
TreasuryTransaction rows Stage 5C's own one-to-one pass ALREADY matched
this run — a transfer relationship is additional context about an
already-matched ledger transaction, never an alternative candidate
pool. If the paired leg belongs to a different legal entity, no group is
created at all (entity isolation is never relaxed for this feature,
regardless of what `transfer_pair_id` says).

## 6. FX matching — architecture

For a bank transaction left with no same-currency candidate at all
(Stage 5C's own candidate generation hard-filters on exact currency
match), a cross-currency candidate is sought in the same entity/account/
compatible-direction/date-window, and converted using an authoritative
FX rate via `select_fx_rate` (Section 7 below).

## 7. FX Rate Selection (Section 16A hardening)

`select_fx_rate` (`reconciliation_advanced_matching.py`) is the single
authoritative, deterministic rate-selection function:

- **Rate type**: read from
  `configuration.matching_rule_config["fx_rate_type"]` if the run's own
  configuration specifies one (validated against the existing
  `FXRateType` enum — an unrecognized value is NO FX MATCH, never a
  silent fallback); otherwise defaults to `SPOT`, matching `FXRate`'s
  own model-level default.
- **Effective date**: only rows with `rate_date <= transaction_date` are
  ever considered — a future-dated rate is never selected, enforced
  directly by the SQL filter. The most recent such date is preferred
  (an exact-date match is simply the closest qualifying row, so no
  separate step is needed) — this is the standard "as of date"
  treasury convention, consistent with the rest of this codebase's own
  historical-reproducibility principles.
- **Maximum staleness**: enforced only if
  `configuration.matching_rule_config["fx_max_staleness_days"]` is set;
  if unset, no arbitrary limit is invented (the existing FX
  architecture defines no such rule itself).
- **Source priority / ambiguity**: `FXRate`'s own identity constraint
  (`uq_fx_rate_identity` on `from_currency_code, to_currency_code,
  rate_type, rate_date, version`) already makes it structurally
  impossible for two `is_current=True` rows to share the same (pair,
  type, date) identity — true "two equally authoritative rates"
  ambiguity is unreachable given this schema. `select_fx_rate` still
  contains a defensive check for this (returning `None` rather than
  picking a database row arbitrarily) so the function remains correct
  even if that constraint is ever relaxed, but this is honestly
  documented as unreachable in normal operation rather than exercised
  by a test that would need to violate the schema's own constraint to
  construct the scenario.
- **Currency-pair direction**: only the DIRECT pair
  (`source_currency -> target_currency`) is ever queried. **No
  inversion is ever attempted** — the `FXRate` model carries no explicit
  flag or documented convention permitting mathematical inversion of a
  stored rate, so per the instruction to invert "only if the existing
  model explicitly permits it," the correct, honest behavior is NO FX
  MATCH when only the reverse pair exists. `FxRateSelection.inverse_used`
  is therefore always `False` in this implementation — the field exists
  so a future stage that adds documented inversion support can populate
  it without a schema change.
- **Triangulation**: never attempted. Only a single direct currency-pair
  query is ever issued — `USD → EUR` and `EUR → NGN` both existing never
  implies a `USD → NGN` match.
- **Provenance**: every FX match group stores `fx_rate_id` (a direct
  reference to the actual `FXRate` row, preferred over copying only the
  numeric value), plus `fx_rate` and `fx_rate_date` as a denormalized
  historical snapshot, `fx_rate_type`/`fx_rate_source` (which type and
  source were actually used), and `fx_tolerance_pct` (the tolerance
  actually applied) — the whole conversion decision is reconstructable
  from the group row alone.
- **Historical reproducibility**: a completed run's FX match group keeps
  referencing its own `fx_rate_id` forever — adding a newer FX rate
  afterward never changes what an already-created group points to
  (verified explicitly).
- **Decimal arithmetic and conversion precision**: `convert_fx_amount`
  computes `(bank_amount * rate)` in `Decimal` throughout (never `float`
  or `round()`) and quantizes to the configured `Currency.decimal_places`
  of the currency the result is **denominated in**. The rate is selected
  as bank currency (source, `from_currency_code`) → ledger transaction
  currency (target, `to_currency_code`) and used directly, so the
  converted amount is in the ledger currency and **that** currency's
  `decimal_places` controls precision. FX conversion never assumes a
  universal two-decimal precision (an earlier version hardcoded
  `Decimal("0.01")`). Rounding is `ROUND_HALF_UP`, the repository's
  existing monetary convention; the earlier code passed no mode and
  inherited the Decimal default (`ROUND_HALF_EVEN`), which differs only on
  exact `.5` ties. FX tolerance is evaluated on the quantized amount;
  rate selection and the tolerance semantics themselves are unchanged.
  For an FX group, the stored `difference` is `|converted - ledger|` in
  the ledger currency (it was previously `|bank - ledger|`, which
  subtracted amounts in two different currencies and was meaningless).
  Known limit: `difference`, `*_aggregate_amount`, and the
  `TreasuryTransaction`/`BankStatementTransaction` amount columns are
  `Numeric(20,2)`, so a currency with more than two decimal places can be
  compared at full precision but not stored beyond two places on those
  columns; the exact quantized conversion is recorded in `reason`.

## 8. FX match decision

```
No candidate at all in a different currency  -> not evaluated (no cross-currency row exists)
No authoritative rate found                  -> NO FX MATCH
Converted amount within configured tolerance  -> FX_MATCH
Converted amount outside configured tolerance -> NO FX MATCH (never forced)
```

FX conversion never overrides entity isolation, bank-account isolation,
direction compatibility, ACTIVE-only bank evidence, claimed-transaction
protection, configuration versioning, or concurrency controls — all of
these are enforced identically for an FX candidate as for any other.

## 9. Configuration

All Stage 5D-specific configuration lives in the existing
`ReconciliationConfiguration.matching_rule_config` JSONB field (Stage
5B's own reserved extensibility column) — no new configuration table or
columns were needed for behavior tuning:

```
advanced_matching_enabled   (bool, default true)
max_group_size              (int, default 5)
max_candidates_evaluated     (int, default 20)
fx_tolerance_pct             (decimal, default 1.0)
fx_rate_type                 (str, default "SPOT")
fx_max_staleness_days        (int, optional — no default, no limit if unset)
```

Every Stage 5D detection function uses the run's own stored
`configuration_id` exactly as Stage 5C does — never re-resolving a newer
configuration version at execution time.

## 10. Claim protection

Reuses Stage 5C's `already_claimed_treasury_transaction_ids` (entity-
scoped, unchanged) as a baseline, but ALSO threads explicit
`extra_excluded_treasury_ids`/`extra_excluded_bank_ids` between the four
Stage 5D phases (one-to-many → many-to-one → internal transfer → FX)
within the same pass. This threading is necessary because
`already_claimed_treasury_transaction_ids` only sees Stage 5C's
`ReconciliationMatchSuggestion` claims — it has no visibility into
`ReconciliationMatchGroup` claims made by an earlier Stage 5D phase in
the same pass. Without this threading, a ledger transaction claimed by
a one-to-many group could then also be swept into a many-to-one group
in the same run — a genuine bug found and fixed during implementation
(covered by the claim-protection test).

**Cross-run claims (found by the Stage 5D cross-run concurrency test).**
The scope advisory lock serializes two same-scope runs correctly, but the
second run's claim lookup could not see ledger/bank rows already consumed
by a `PENDING`/`ACCEPTED` match group from the first run, so both runs
grouped the same rows. `claimed_ids_from_existing_groups` now adds those
claims (entity-scoped via SQL joins to `TreasuryTransaction`/
`BankStatementTransaction`) as the baseline exclusion for every phase.
`AMBIGUOUS` and `REJECTED` groups claim nothing, mirroring Stage 5C's
rule that an unresolved tie selects no single candidate. Stage 5C's own
helper is unchanged.

## 11. Idempotency

`ReconciliationMatchGroup.group_key` — a deterministic string built from
`(relationship_type, sorted bank member IDs, sorted ledger member IDs)`
— backed by a database unique constraint on
`(reconciliation_run_id, group_key)`. Additionally, per-phase logic
ensures a repeated advanced-matching pass over the same run never
re-derives a different or duplicate outcome.

## 12. Concurrency

Reuses Stage 5C's locking completely unchanged (and, per the cross-run
claim fix above, extends claim visibility to match groups). Verified
against the advanced pass specifically: same run executed concurrently
-> exactly one succeeds and exactly one group exists; two different runs
with the identical scope -> both complete, no ledger row appears in more
than one group; two independent scopes -> both execute concurrently.
Locking itself: the run's own row
lock (`load_run_for_update`) protects same-run concurrent execution, and
the PostgreSQL advisory transaction lock
(`acquire_reconciliation_scope_lock`) protects different runs covering
the identical (entity, account, period) scope. Stage 5D introduces no
new locking mechanism and no global application lock — independent
scopes remain fully concurrent.

## 12a. Scale test (measured, not a benchmark claim)

`test_full_run_at_scale_10k_ledger_1k_bank_stays_bounded` seeds 10,000
unrelated ledger rows and 1,000 unrelated bank rows plus one genuine
reference-linked grouping, executes the run end to end, and requires
exactly one group. On the development container the execute took roughly
20-26 seconds, most of it Stage 5C's per-bank-transaction candidate loop;
this is informational only (nothing asserts on time) and says nothing
about production hardware. The point of the test is correctness under
volume: the real group is found among the noise and none is invented
from it. Stage 5D's own queries are narrowed by reference in SQL.

## 13. Security

Cross-group isolation is tested for both the `match-groups` endpoint (a
user in another group gets 403) and the matching itself (a same-
reference ledger set in another group never contributes to a group).
Every Stage 5D query includes `legal_entity_id` and `bank_account_id` as
hard, non-relaxable predicates — verified explicitly for grouping (an
identical-reference ledger row in a different entity is never fetched),
FX matching (a cross-entity candidate is never evaluated even when an
authoritative rate exists), and internal transfers (a cross-entity
paired leg is never described as a transfer). Non-FX grouping never
crosses currency either.

## 14. Financial side effects

Confirmed by dedicated tests: Stage 5D never modifies a
`TreasuryTransaction`'s own fields, never modifies `BankBalance`, and
never creates a new `TreasuryTransaction`/`BankStatementTransaction` row
— it only reads existing evidence and writes
`ReconciliationMatchGroup`/`ReconciliationMatchGroupMember` rows.

## 15. Database changes

One migration: `reconciliation_match_groups` +
`reconciliation_match_group_members` (with the exactly-one-side check
constraint on member rows and the group-identity unique constraint),
two run-level summary counters
(`advanced_match_count`/`ambiguous_advanced_count`), and FX provenance
columns (`fx_rate_id`/`fx_rate_type`/`fx_rate_source`/`fx_tolerance_pct`)
on `reconciliation_match_groups`.

## 16. API

No new execution endpoint — `POST /reconciliation/runs/{id}/execute`
now runs Stage 5C's one-to-one pass followed by Stage 5D's advanced pass
within the same transaction. One new minimal read-only endpoint,
`GET /reconciliation/runs/{id}/match-groups`, entity-scoped identically
to every other run-scoped endpoint.

## 17. Known limitations

- Grouping evidence is reference-based only — an aggregate match with
  no shared reference at all (and no batch keyword) is never grouped,
  even if the amounts genuinely correspond. This is intentional
  (Section 8/12 of the spec: never infer solely from amount).
- `BATCH` classification relies on a "BATCH" keyword in the bank
  transaction's own reference/narration — there is no dedicated
  batch-identifier field in the current `BankStatementTransaction`
  schema to detect this more precisely.
- FX rate inversion and triangulation are not supported, by design —
  the current `FXRate` model has no documented convention for either.
- The "ambiguous FX rate" defensive branch in `select_fx_rate` is
  unreachable in normal operation, since `FXRate`'s own unique
  constraint already prevents the scenario it guards against.

## 18. Explicit Stage 5E+ deferrals

Open-item workflow, task assignment, reconciliation reports, adaptive
learning/AI matching, and the full reconciliation frontend are **not
implemented** in Stage 5D.
