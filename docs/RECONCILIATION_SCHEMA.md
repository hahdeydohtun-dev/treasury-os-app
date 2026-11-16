# Reconciliation schema overview (Stages 5A–5E)

One page that maps every table the bank-reconciliation stages added, how they relate, and which database
constraints carry the integrity guarantees. It is a map, not a replacement for the stage documents, which hold the
detail and the rationale:

| Stage | Document |
|---|---|
| 5A bank statement ingestion | `STAGE_5A_BANK_STATEMENT_INGESTION.md` |
| 5B data model and run lifecycle | `STAGE_5B_RECONCILIATION_DATA_MODEL.md` |
| 5C deterministic matching | `STAGE_5C_DETERMINISTIC_MATCHING_ENGINE.md` |
| 5D advanced / FX matching | `STAGE_5D_ADVANCED_MATCHING.md` |
| 5E open items, decisions, duplicate-acceptance protection | `STAGE_5E_OPEN_ITEM_WORKFLOW.md` |
| 5F reports and exports (read-only, no tables of its own) | `STAGE_5F_RECONCILIATION_REPORTS.md` |
| Test database safety | `TEST_DATABASE_SAFETY.md` |

Facts below were taken from the models and migrations at the Stage 5E verification state (Alembic head
`d5e508d02a28`, 25 migrations in one linear chain). If a table or constraint changes, update this page in the same
change.

## Tables

| Table | Introduced | Purpose | Named constraints / indexes |
|---|---|---|---|
| `bank_statement_transactions` | 5A | Normalized **external bank evidence**. Never a cash transaction and never changes a balance. Status `ACTIVE` / `REVERSED`; only `ACTIVE` rows are evidence. | partial unique `ux_bank_statement_transactions_strong_duplicate_key` |
| `reconciliation_configurations` | 5B | Versioned per-entity matching configuration (tolerances, thresholds, `matching_rule_config` JSONB). | `uq_reconciliation_configuration_identity` |
| `reconciliation_runs` | 5B | One scoped job (entity, bank account, period) with its lifecycle and result counters. | – |
| `reconciliation_match_suggestions` | 5C | **One-to-one proposals** from the deterministic engine, with confidence, reason and rule version; also a "no candidate" placeholder per unmatched bank row. | partial unique `ux_reconciliation_match_suggestions_identity`, `ux_reconciliation_match_suggestions_no_candidate` |
| `reconciliation_match_groups` | 5D | **Grouped proposals**: one-to-many, many-to-one, batch, internal transfer, FX match. Carries FX provenance (rate, type, source, date, tolerance). | `uq_reconciliation_match_group_identity` |
| `reconciliation_match_group_members` | 5D | The bank and/or ledger transactions in a group. | `ck_reconciliation_match_group_member_exactly_one_side` |
| `reconciliation_open_items` | 5B, extended 5E | **Unresolved exceptions** needing investigation, with workflow state (assignee, priority, due date, resolution). | `uq_reconciliation_open_item_source` (one item per run per exception) |
| `reconciliation_open_item_events` | 5E | **Append-only history** of each open item (comments, assignments, evidence references, every status change). No endpoint updates or deletes it. | – |
| `reconciliation_match_decisions` | 5E | The **explicit accept / reject decision** on a proposal, with actor, time, reason and previous/new status. | `ck_reconciliation_match_decision_exactly_one_subject`, `uq_reconciliation_match_decision_suggestion`, `uq_reconciliation_match_decision_group` (one decision per proposal) |
| `reconciliation_accepted_claims` | 5E | One row per transaction per **accepted** proposal; the guarantee that a transaction is never accepted through two proposals, across runs and under concurrent requests. | `ck_reconciliation_accepted_claim_exactly_one_transaction`; partial unique `ux_accepted_claim_bank`, `ux_accepted_claim_ledger` on `(transaction, claim_kind)` |

## Status vocabularies

| Enum | Values |
|---|---|
| `ReconciliationRunStatus` | `DRAFT`, `READY`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `MatchSuggestionStatus` | `PENDING`, `ACCEPTED`, `REJECTED`, `SUPERSEDED` |
| `MatchGroupStatus` | `PENDING`, `AMBIGUOUS`, `ACCEPTED`, `REJECTED` |
| `MatchRelationshipType` | `ONE_TO_MANY`, `MANY_TO_ONE`, `BATCH`, `INTERNAL_TRANSFER`, `FX_MATCH` |
| `OpenItemStatus` | `OPEN`, `UNDER_REVIEW`, `RESOLVED`, `CLOSED`, `REJECTED` |
| `OpenItemCategory` | `BANK_ONLY`, `LEDGER_ONLY`, `AMOUNT_VARIANCE`, `TIMING_DIFFERENCE`, `DUPLICATE`, `BANK_CHARGE`, `INTERNAL_TRANSFER`, `FX_DIFFERENCE`, `ONE_TO_MANY`, `MANY_TO_ONE`, `BATCH_PAYMENT`, `MISSING_LEDGER_ENTRY`, `MISSING_BANK_ENTRY`, `EXCEPTION`, `AMBIGUOUS_MATCH` |
| `OpenItemEventType` | `CREATED`, `ASSIGNED`, `REASSIGNED`, `INVESTIGATION_STARTED`, `COMMENT`, `EVIDENCE`, `RESOLVED`, `CLOSED`, `REJECTED`, `REOPENED` |
| `MatchProposalKind` / `MatchDecisionType` / `AcceptedClaimKind` | `SUGGESTION`, `GROUP` / `ACCEPTED`, `REJECTED` / `MATCH`, `TRANSFER` |

Not every `OpenItemCategory` is produced by the engines; `DUPLICATE`, `BANK_CHARGE`, `FX_DIFFERENCE` and the
`MISSING_*` values are reserved vocabulary (see the Stage 5E document).

## How the pieces relate

```
BankStatementTransaction (ACTIVE) ─┐
                                   ├─► run ─► ReconciliationMatchSuggestion  (PENDING) ─┐
TreasuryTransaction (POSTED) ──────┘      └─► ReconciliationMatchGroup + members (PENDING / AMBIGUOUS) ─┤
                                                                                                         │ proposals
   explicit, authorized decision ◄───────────────────────────────────────────────────────────────────────┘
        │ accept  ─► status ACCEPTED + ReconciliationMatchDecision + ReconciliationAcceptedClaim rows
        │ reject  ─► status REJECTED + ReconciliationMatchDecision (transactions become unmatched again)
        ▼
ReconciliationOpenItem  ◄── derived idempotently per run from unmatched / ambiguous / weak-evidence results
        └─► ReconciliationOpenItemEvent (append-only history)
```

* **PENDING is a proposal, not a reconciliation.** The engines never accept anything; only an explicit decision sets
  `ACCEPTED` or `REJECTED`.
* Proposals that are ambiguous (ties, competing groups) or weak-evidence are exceptions to investigate, not
  decidable proposals.
* A transaction can be part of at most one **accepted** proposal per namespace (`MATCH`, or `TRANSFER` for internal
  transfer groups). Two overlapping `PENDING` proposals may coexist; accepting one never accepts or rejects the other.

## Cross-cutting guarantees

* **No financial side effects.** Nothing in reconciliation creates, changes or deletes a `TreasuryTransaction`,
  a `BankStatementTransaction` or a `BankBalance`. Resolving an item or accepting a match is a decision, not a
  posting.
* **Persisted monetary precision is 0–2 decimal places** (`Currency.decimal_places` is constrained to 0–2);
  amounts the ledger cannot hold are rejected, never rounded.
* **Entity and group isolation** on every list and detail query; an item or proposal outside the caller's scope is
  reported as not found.
* **Idempotent derivation.** Re-synchronizing a run creates no duplicate items, events or audit records, and never
  resets resolved or closed items.
* **Authorization** reuses the existing `BANK_RECONCILIATION` permissions (`VIEW`, `ASSIGN`, `INVESTIGATE`,
  `COMMENT`, `RESOLVE`, `CLOSE`, `EXECUTE`, and `APPROVE` for accept / reject decisions).

## API and pages

* API prefixes: `/reconciliation` (runs, configurations, suggestions, groups), `/reconciliation/open-items`,
  `/reconciliation/match-proposals`, `/reconciliation/reports` (Stage 5F, read-only). Endpoint tables are in the Stage 5B and 5E documents.
* Frontend pages: `/reconciliation` (runs), `/reconciliation/open-items` (exceptions work queue) and
  `/reconciliation/match-review` (proposals awaiting a decision) and `/reconciliation/reports` (Stage 5F reports and
  exports). The full reconciliation frontend is Stage 5H and is not built.
