# Stage 5G — Adaptive Matching (v1)

Adaptive matching uses the matches your team has **explicitly accepted or rejected** to recommend which Stage 5C suggestions
deserve attention first, and explains why. It is **off by default**, it only annotates, and **a person must still accept or
reject every match**. It never changes a suggestion, its deterministic confidence, its tier or whether it can be decided.

## 1. Scope of the first release

| In scope (v1) | Excluded in v1 (continues unchanged through the existing deterministic process) |
|---|---|
| Stage 5C **one-to-one** suggestions: learning from their accepted / rejected decisions and scoring them | Stage 5D **one-to-many, many-to-one, batch-payment and internal-transfer** proposals: not learned from, not scored |
| **Account-level** learning only: entity, bank account, currency, transaction direction and match type | Bank-wide, entity-wide or group-wide learning; cross-account sharing of any kind |
| Ranking and explaining **already-persisted** suggestions | **Candidate re-selection**: Stage 5C candidate generation, selection and tie handling are untouched |

"Repeated payment batch patterns" need group evidence and are therefore deferred. Group decisions are counted in the learning-health
view as *not learned from in v1* so the exclusion is visible, never silent.

## 2. Architecture

```
Stage 5C one-to-one engine  ─┐
Stage 5D advanced matching  ─┴─►  persisted suggestions (PENDING)
                                        │
        annotate_run_adaptively()  ◄────┘   (one hook in execute_reconciliation_run, after 5D, before the 5E open-item sync)
                │  reads: decisions + suggestions + the two transactions, strictly within one entity and bank account
                ▼  writes: one immutable row per suggestion in reconciliation_adaptive_scores   (nothing else)
Stage 5E review queue / accept / reject / claims / open items   ── unchanged ──
```

| File | Role |
|---|---|
| `models/reconciliation_adaptive.py`, migration `a7c1e9b4d2f6` | the one new table, with its constraints and triggers |
| `services/reconciliation_adaptive_settings.py` | settings, defaults, hard bounds, fail-safe parsing |
| `services/reconciliation_adaptive_signals.py` | pure signal extraction (no I/O, no clock) |
| `services/reconciliation_adaptive_learning.py` | evidence eligibility, aggregation, scoring, tier-lock |
| `services/reconciliation_adaptive_service.py` | annotation, failure isolation, audit, explanation payloads |
| `api/v1/reconciliation_adaptive.py` | settings, learning health, explanations |

The hook in `execute_reconciliation_run` is five lines. It cannot fail a run: annotation runs in a savepoint, never raises, and on any
error writes only an audit record. When adaptive matching is disabled it does nothing at all.

## 3. Learning evidence

**Source.** Evidence is read directly from the authoritative Stage 5E records: `reconciliation_match_decisions`, joined to the
suggestion it decided, its run, and its bank and ledger transactions. Nothing is copied into a second dataset, so a learning input is
always traceable to its original decision, run, decider and timestamp.

**Eligible.** A decision is evidence only if **all** hold: it is an explicit `ACCEPTED` or `REJECTED` decision on a one-to-one suggestion;
its recorded outcome, the suggestion's status and its run agree; the suggestion has both transactions, a confidence and a match type; the
run is `COMPLETED`; both transactions belong to this entity and this bank account, in one currency; the bank row is still `ACTIVE` and the
ledger row still `POSTED`; an accepted decision still has its claim; and it was made **before** the evidence cutoff and inside the age window.

**Excluded, and counted by reason** (shown in the learning-health view, never silently dropped):
`GROUP_PROPOSAL_NOT_LEARNED_IN_V1`, `SUGGESTION_MISSING`, `INVALID_DECISION_RECORD`, `STATUS_MISMATCH`, `INCOMPLETE_SUGGESTION`,
`RUN_MISMATCH`, `RUN_NOT_COMPLETED`, `TRANSACTION_MISSING`, `SCOPE_MISMATCH`, `CURRENCY_MISMATCH`, `BANK_TRANSACTION_REVERSED`,
`LEDGER_TRANSACTION_NOT_POSTED`, `ACCEPTED_WITHOUT_CLAIM`, `OUTSIDE_EVIDENCE_WINDOW`, `SUPERSEDED_BY_LATER_DECISION_ON_SAME_PAIR`.

**PENDING never counts.** A pending proposal has no decision, so it cannot appear in the decision-based evidence at all. **Rejected
decisions are only ever negative evidence**: they lower a pattern's balance and can never raise a score.

**Evidence independence: one underlying pair is one observation.** Stage 5E lets a rejected pair return to the unmatched pool, so the same
bank transaction and ledger transaction can be proposed, and rejected, again in every later run (verified with the real engine: the same pair
was proposed and decided in three consecutive runs). Those decision records are not independent examples of a pattern. Without a rule, five
rejections of one pair across five runs would look like five examples from five runs and could satisfy `min_evidence` and `min_distinct_runs`
by themselves. The policy:

* An **observation** is identified by *(bank transaction, ledger transaction, currency, direction, match type)* within the entity and bank
  account. Each observation is counted **at most once** for a learning cycle, in `n`, in the accepted and rejected counts, in the
  leave-one-out stability check and in the pattern balance.
* Among a pair's **eligible** decisions (the cutoff, age window and every eligibility check above still apply first) the **latest** is
  authoritative; ties on the timestamp are broken by decision id, so the choice is deterministic. The observation belongs to that decision's
  run, so superseded decisions add no phantom runs to `min_distinct_runs`.
* The earlier decisions are **never deleted or altered**. They are counted as `SUPERSEDED_BY_LATER_DECISION_ON_SAME_PAIR`, and their ids are
  kept with the observation (`superseded_decision_ids`) and on the stored explanation, so everything stays traceable to the authoritative
  Stage 5E records.
* **Conflicting histories** are handled by the same rule and reported, not hidden. Accepted pairs are claimed and cannot be proposed again,
  so a conflict in practice is *rejected, then accepted*; the later decision is the human's current judgement and decides. Such pairs are
  counted in `pairs_with_conflicting_dispositions` (health view and the stored settings snapshot).
* The same pair decided under different match types is two observations, because evidence is only ever combined within one match type.

**Corrected or invalidated decisions.** Stage 5E has no decision-reversal workflow (decisions are final), so none is invented. The
existing supported ways a decision's basis can be invalidated are handled: a bank row later marked `REVERSED` or a ledger row no longer
`POSTED` drops the decision out of future evidence (and is counted).

## 4. How a suggestion is scored

Every pair, historical or new, is reduced to up to four **independent signal families**; a family is omitted when its inputs are missing:

| Family | What it describes | Example key |
|---|---|---|
| `date_offset` | signed settlement lag, bank date minus ledger date (within 10 days) | `+1d` |
| `reference_format` | the shape of the two references and how they relate | `AAA999999999>AAA-99999:different` |
| `payee_pattern` | informative words of the bank narration paired with the ledger counterparty | `nip transfer from acme>acme ltd` |
| `amount_relation` | exact, or a signed difference bucket (`bank_lower:<=0.5%`, ...) | `exact` |

No family is "amount similarity" or "description similarity" on its own, and a contribution needs **at least two independent families**.

For each pattern (a family key within one scope) with weighted accepted `A` and rejected `R` evidence:

* **Supported** only if it has at least `min_evidence` decisions, from at least `min_distinct_runs` runs, a non-zero balance, and its
  direction does not depend on any single decision (removing any one decision must not flip or zero it).
* `balance = (A − R) / (A + R + 2)`, always strictly between −1 and +1. The `+2` shrinks thin evidence toward zero.
* Evidence weight halves for every full `half_life_days` of age (an exact step function); decisions beyond `max_evidence_age_days` are ignored.
* Each supported family contributes `share × cap × balance`, with `cap = max_boost` if the balance is positive and `max_penalty` if
  negative, and shares `date_offset 0.2, reference_format 0.3, payee_pattern 0.3, amount_relation 0.2`.
* The total is clamped to `[−max_penalty, +max_boost]` and rounded half-up to 2 decimals. All arithmetic is `Decimal`.

**Support level**: `LOW` = enough supported families; `MEDIUM` = three or more families, or two with at least double the minimum
evidence; `HIGH` = three or more families, each with at least double the minimum evidence, and a mean absolute balance of at least 0.5.

**Fallback to the deterministic score** happens (and is recorded with its reason) when no pattern is supported, when fewer than
`min_signal_families` are supported, or when the support level is below the configured minimum. The adaptive contribution is then 0 and
the recommendation equals the deterministic score.

### Tier-lock (decision integrity)

`recommendation = clamp(deterministic + adaptive)` **inside the deterministic score's own tier**: a score at or above the auto-match
threshold (70) stays in `[70, 100]`; one below it stays in `[0, 69.99]`. So 69 + 10 gives **69.99** (it cannot reach the auto tier) and
70 − 10 gives **70.00** (it cannot fall out of it). Tests cover the boundary at 69 and 70 and every score from 0 to 100 against every
contribution from −15 to +15.

### What is never changed

The suggestion's `confidence`, `status`, `reason`, `match_type`, rule version, references and **decidability** are not touched. The
Stage 5E predicate that decides what can be accepted is not changed or re-implemented, and adaptive scores are never an input to it.
Ties, review-tier suggestions and placeholders stay non-decidable whatever the evidence (tested). Ties are decided on the deterministic
score, as before.

## 5. Isolation

Evidence is selected for **one entity and one bank account** (the run's own), and is only ever combined within the same currency,
transaction direction and match type. There is no group-level, entity-level or cross-account evidence. Isolation is enforced by the evidence
query, by an independent per-decision scope check, and in the database: a trigger refuses a stored explanation whose entity, bank
account or run is not the suggestion's own. Tests prove entity A cannot influence entity B (both directions), account A cannot influence
account B of the same entity, and currency, direction and match type are separate scopes.

## 6. Settings and administration

Settings live in the existing versioned reconciliation configuration under `matching_rule_config["adaptive"]`. **Absent means disabled.**

| Setting | Default | Allowed |
|---|---|---|
| `enabled` | `false` | true / false |
| `scope` | `account` | `account` only |
| `min_evidence` | 5 | 3–100 |
| `min_distinct_runs` | 2 | 2–20 |
| `min_signal_families` | 2 | 2–4 |
| `max_boost` / `max_penalty` | 10 / 10 | 0–15 (a database CHECK also enforces ±15) |
| `half_life_days` | 180 | 30–730 |
| `max_evidence_age_days` | 540 | 30–1095, not below the half-life |
| `min_support_level` | `LOW` | LOW / MEDIUM / HIGH |

* `GET/PUT /reconciliation/adaptive/settings` (`VIEW` / `CONFIGURE`). A save creates a **new configuration version** through the existing
  `create_configuration_version` and is audited as `ADAPTIVE_CONFIGURE` with the previous and new settings.
* The new version is a **fork of the configuration runs use today** (every tolerance and other key is carried over), so enabling or tuning
  adaptive matching can never silently change an account's matching behaviour. Tested.
* The endpoint rejects out-of-range or unknown values with 422. Because `matching_rule_config` is free-form JSON that the generic
  configuration endpoint also accepts, the **runtime also clamps** any out-of-range value to its bound and treats unparsable or
  non-`account` scope settings as **disabled**.
* `GET /reconciliation/adaptive/health` shows, for one account, eligible and excluded evidence, patterns supported per family and how
  recent suggestions were scored. `GET /reconciliation/adaptive/explanations` returns explanations only for suggestions the caller may
  view; an inaccessible or unknown id is simply absent. Decision references are returned only with `CONFIGURE`.

## 7. Persistence, versioning and reproducibility

One new table, `reconciliation_adaptive_scores`, one row per scored suggestion (unique on the suggestion): deterministic score,
adaptive contribution, recommendation, tier band, fallback flag and reason, support level, accepted and rejected example counts, the
learning-rule and base-rule versions, the configuration id and version, the evidence cutoff, a snapshot of the effective settings, the
evidence-scan counts, and per-pattern evidence (labels, counts, balances, contributions, and the original decision ids).

Why a new table: structured, queryable, immutable and one-to-one with a suggestion, and the alternative (a column on the frozen Stage 5C
suggestions table) would alter a frozen model.

* **Immutable.** A database trigger refuses `UPDATE` and `DELETE`. CHECK constraints refuse impossible values (contribution beyond ±15,
  a fallback row with a non-zero contribution, an unknown tier or support level, negative counts).
* **New decisions affect only future runs.** The evidence cutoff is the run's `started_at`; only decisions made strictly before it are
  used. A stored explanation is never recomputed or edited. Annotation skips suggestions that already have a row, so re-running a run
  does not recompute history. Tested.
* **Reproducible.** From a stored row alone (its cutoff, settings snapshot and the authoritative decisions) the same contribution and
  recommendation are recomputed exactly. Tested.
* **Disabling keeps history.** Disabling writes no new rows and erases nothing; earlier explanations remain readable.

## 8. Human approval and audit

Adaptive matching has no accept or reject path, no scheduler and no automatic decision. Acceptance and rejection are the existing Stage 5E
endpoints, with their eligibility checks, accepted-claim uniqueness and concurrency protection untouched (tested, including the competing
acceptance, which still returns 409). The UI states on every explanation that it is a suggestion requiring human review and offers no
control that accepts anything.

Audit events: `ADAPTIVE_CONFIGURE` (settings change, previous and new values, reason), `ADAPTIVE_ANNOTATE` (per run: counts, versions,
cutoff) and `ADAPTIVE_ANNOTATE_FAILED`. They contain no transaction references, amounts or narrations.

## 9. Failure and fallback

Disabled, absent, unparsable or out-of-scope settings → deterministic matching only. Insufficient or unstable evidence → fallback row with
its reason. Any exception in annotation → contained in a savepoint, audited, and the run completes with its deterministic results intact
(tested through the real execution endpoint).

## 10. The review experience

Match review shows, for a one-to-one suggestion, the deterministic score, the adaptive contribution and the recommendation separately,
the support level, evidence sample sizes, rule and configuration versions, the evidence cutoff, the scope, each learned pattern and
whether it was used, and whether the engine fell back. The queue can optionally be ordered by recommendation (`order_by=recommendation`);
the default order and membership are unchanged. `/reconciliation/adaptive` provides the settings and learning-health view.

## 11. Known limitations

* v1 scope as in section 1: groups are not learned from or scored; no bank-wide or group-wide learning; no candidate re-selection.
* No decision-reversal workflow exists, so evidence is only invalidated through the transaction-status checks above.
* Evidence is derived live from authoritative rows. A stored explanation never changes, but recomputing one after the underlying
  transactions were edited could differ. (No production code path edits their narration, counterparty or reference fields.)
* The evidence scan covers the most recent 5,000 decisions per account (flagged in the health view if reached).
* Independence is by underlying **pair**. The same bank row paired with different ledger rows in different runs (or the reverse) counts as
  separate observations, because they are different proposals.
* Explanations stored before the evidence-independence correction (commit `16618ca`) were computed with per-decision counting. They are
  immutable and are deliberately **not recomputed**; only runs executed afterwards use the corrected policy.
* Runs executed before adaptive matching was enabled have no explanations; there is no re-run or backfill.
* Ties, review-tier suggestions and placeholders receive informational scores but remain exceptions; they are not decidable.
* Learned pattern labels (for example a payee template) are visible to anyone with `VIEW` on that entity, but never any transaction
  reference, amount, narration or another entity's data.
* An entity-level configuration enabling adaptive matching covers every account of the entity; learning remains per account.
* Support-level and weighting rules are fixed heuristics, versioned as `5G-1.0`; changing them requires a new version.
* Migration note: the Stage 5E migration tests previously upgraded to "head" and asserted it was the Stage 5E revision; with any later
  migration that is no longer true, so they now upgrade to the exact revision under test (assertions unchanged).

## 12. Test evidence

PostgreSQL 16.15, using the documented disposable database (`docs/TEST_DATABASE_SAFETY.md`); the safety guard was not changed.

* New tests (139): pure logic 64, learning evidence 19, evidence independence 11, scoring / workflow / versioning 30, API 11, migration 4.
* Covered: eligibility and every exclusion reason; traceability; cutoff; isolation by entity, account, direction, match type and group;
  boosts, penalties, fallback, cap, anomaly resistance; tier-lock on real rows at 69 and 70; ties, review-tier and placeholders stay
  non-decidable; explicit accept and reject through Stage 5E; claim uniqueness (409); no financial side effects; immutability and scope
  triggers; reproducibility; new decisions affect only future runs; disabling keeps history; failure containment; the real engine end to
  end; settings RBAC, versioning, audit and the configuration fork; scoped explanations; opt-in queue ordering; migration upgrade,
  downgrade and data preservation.
* Mutation checks: nine safeguards were deliberately broken (account isolation, entity isolation, tier-lock, minimum evidence, the
  single-decision stability check, rejections-as-accepted, the contribution cap, idempotency, and the enabled-by-default setting). Each
  was caught by a test, after strengthening two tests that initially did not catch their mutation. The evidence-independence correction
  has its own mutation checks (no de-duplication, earliest instead of latest decision, match type dropped from the pair key), all caught.

## 13. Operational guidance

1. Leave adaptive matching disabled until a bank account has a meaningful decision history (at least `min_evidence` decisions per pattern
   across two or more runs). The health view shows how many patterns are usable.
2. Enable it per bank account at `/reconciliation/adaptive` (needs `CONFIGURE`). It affects only runs executed afterwards.
3. Treat the recommendation as prioritisation. Review and decide as you always do; check the explanation if the recommendation surprises you.
4. To stop, disable it: new runs revert to deterministic scoring immediately and earlier explanations remain for audit.
