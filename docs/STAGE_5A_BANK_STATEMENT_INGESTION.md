# Stage 5A — Bank Statement Ingestion & Normalization

## 1. Scope

Stage 5A creates a clean, normalized, auditable evidence layer for bank
statement data. It does NOT implement matching, reconciliation, open
items, or reports - those are Stage 5C, 5D/5E, and 5F respectively (see
the Stage 5 forensic integration report for the full sub-stage plan).

**What exists after Stage 5A**: bank statement upload → validation →
normalization → duplicate detection → entity/RBAC authorization →
durable, traceable `BankStatementTransaction` evidence, ready to be
consumed by a future matching stage.

**What does NOT exist yet**: candidate generation, match scoring,
confidence scoring, AUTO_MATCH/REVIEW_REQUIRED, one-to-many/many-to-one
matching, batch payment matching, internal transfer matching, bank-charge
matching, FX matching, adaptive weights, match decisions,
`reconciliation_open_items`, assignment/aging/resolution workflow,
reconciliation reports, report versions/approval/finalization, or any
task/workflow system (see Section 11 below for why).

## 2. The distinction: BankStatementTransaction ≠ TreasuryTransaction

| | `TreasuryTransaction` | `BankStatementTransaction` |
|---|---|---|
| Represents | Treasury OS's own internal cash event | What the bank itself reported happened |
| Created by | Every cash-affecting module (investments, facilities, manual entry) | Only the Bank Statement Excel import |
| Affects operational cash | Yes | **No, never** |
| Affects `BankBalance` | No (read-only source) | No |
| Direction vocabulary | `CashDirection` (INFLOW/OUTFLOW) | `BankStatementEntryType` (DEBIT/CREDIT) - the bank's own vocabulary, deliberately kept separate |
| Mutability once posted | Never edited in place | Never edited in place (a future correction workflow, not built in Stage 5A, would use `BankStatementTransactionStatus.REVERSED`, reserved but unused) |

Stage 5A introduces **no second cash ledger**. `BankStatementTransaction`
has zero effect on `calculate_operational_available_cash`,
`BankBalance`, or any investment/facility cash calculation - verified
explicitly by
`test_confirmed_import_creates_statement_transactions_and_never_treasury_transactions`,
which asserts that after a successful import, zero `TreasuryTransaction`
rows exist for the account.

## 3. Data model

`app/models/bank_statement.py::BankStatementTransaction`:

`legal_entity_id`, `bank_id`, `bank_account_id`, `statement_period_start`,
`statement_period_end`, `transaction_date`, `value_date`, `posting_date`,
`entry_type` (DEBIT/CREDIT), `amount` (always positive - Decimal, never
float), `currency_code`, `balance_after_transaction`, `bank_reference`,
`external_transaction_id`, `narration`, `duplicate_key`,
`has_strong_identity`, `status` (ACTIVE/REVERSED), `import_batch_id`,
`source_row_number`.

Follows every existing Treasury OS convention: UUID primary key
(`UUIDPKMixin`), `created_at`/`updated_at` (`TimestampMixin`), FK-based
entity/bank/account/currency/import-batch ownership, indexed on every
column a future matching stage will filter by (`bank_account_id`,
`legal_entity_id`, `transaction_date`, `value_date`,
`external_transaction_id`, `bank_reference`, `currency_code`,
`import_batch_id`, `duplicate_key`).

## 4. Excel Data Hub integration

A new `BANK_STATEMENT` template (`app/services/bank_statement_excel_template.py`)
registered into the existing `TEMPLATE_REGISTRY` - **zero changes to the
generic upload/validate/preview/confirm engine**
(`app/services/excel_service.py`). Required columns: Entity, Bank
Account, Statement Period Start, Statement Period End, Transaction Date,
Debit/Credit, Amount, Currency. Optional: Value Date, Posting Date,
Balance, Bank Reference, External Transaction ID, Narration.

**Design note on statement period**: the spec's own field table lists
Statement Period Start/End as batch-level concepts, but Treasury OS's
Excel Data Hub is a row-based validation engine with no batch-level
validation hook. Rather than modify the shared engine (explicitly
prohibited), Statement Period Start/End are required on every row - a
common real-world pattern for consolidated multi-account/multi-period
exports, and one that lets every validation rule (period ordering,
transaction-date-within-period) run through the existing row-based
`validate_row` mechanism with no core changes.

Bank-agnostic by design: no bank's statement format is hard-coded. A
bank account is identified by its account number (already unique per
`BankAccount.account_number`), and every date/amount/debit-credit field
is normalized (Section 6) rather than assumed to arrive in one fixed
format.

## 5. The precise import lifecycle

Uses the EXISTING `ImportBatchStatus` enum unchanged (`UPLOADED`,
`VALIDATING`, `VALIDATED`, `READY_FOR_IMPORT`, `IMPORTING`, `IMPORTED`,
`PARTIALLY_IMPORTED`, `FAILED`, `CANCELLED`) - it already covered every
state Stage 5A's own lifecycle specification needed, so no new status
values were introduced.

**Upload is not import**: `POST /excel/uploads` runs
UPLOAD→VALIDATING→(FAILED|READY_FOR_IMPORT) and creates *zero*
`BankStatementTransaction` rows - verified by
`test_upload_alone_creates_no_durable_statement_transactions`. Rows only
become durable evidence on explicit confirmation:
`POST /excel/imports/{id}/confirm` (READY_FOR_IMPORT → IMPORTING →
IMPORTED/PARTIALLY_IMPORTED/FAILED).

**Row outcome classification** during validation, all recorded as
`ImportIssue` rows for full traceability:
- `ACCEPT` (no issue raised) - imported.
- `REJECT` (ERROR severity - `INVALID_DATE`, `DATE_OUTSIDE_STATEMENT_PERIOD`,
  `INVALID_AMOUNT`, `INVALID_CURRENCY`, `INVALID_DEBIT_CREDIT`,
  `BANK_ACCOUNT_NOT_FOUND`, `BANK_ACCOUNT_NOT_AUTHORIZED`,
  `ENTITY_NOT_FOUND`, `MISSING_REQUIRED_FIELD`,
  `INVALID_STATEMENT_PERIOD`) - never imported.
- `EXACT_DUPLICATE` (ERROR severity - `DUPLICATE_TRANSACTION`, or
  `DUPLICATE_ROW` for a within-file duplicate) - never imported.
- `POTENTIAL_DUPLICATE` (WARNING severity) - **imported**, flagged for
  visibility, never silently discarded (Section 8 below).
- `WARNING` (e.g. `STATEMENT_PERIOD_OVERLAP`) - imported, flagged.

**Transactional import**: `confirm_import`'s per-row loop either creates
the `BankStatementTransaction` row and counts it, or skips and counts it
as skipped - the final `imported_rows`/`skipped_rows` counts and the
batch's final status are always consistent with what was actually
persisted (verified by `test_mixed_valid_invalid_file_partially_imports`
and `test_confirmed_import_creates_statement_transactions_and_never_treasury_transactions`).

**Idempotency and concurrency** (a real gap found and fixed): the
generic `confirm_import` had no row lock on the `ImportBatch` before
transitioning it - two concurrent confirmations of the same batch could
both have read `READY_FOR_IMPORT` before either committed. Fixed by
`app/services/bank_statement_service.py::load_import_batch_for_update`
(`SELECT ... FOR UPDATE`), used by the confirm endpoint - this closes the
gap for every template's import confirmation, not only
`BANK_STATEMENT`. Verified with a genuine concurrent-request test
(`asyncio.gather`, not sequential calls):
`test_concurrent_confirmation_cannot_duplicate_records` - exactly one of
two simultaneous confirmations succeeds, never both. Repeated (sequential)
confirmation of an already-`IMPORTED` batch is rejected (400) by the
existing status guard, unchanged -
`test_repeated_confirmation_of_same_batch_is_idempotent`.

## 6. Normalization

- **Dates**: the shared `_parse_date` helper (ISO format, native
  openpyxl date/datetime objects) is tried first; a bank-statement-specific
  fallback (`_parse_statement_date` in `bank_statement_excel_template.py`)
  additionally handles DD/MM/YYYY, MM/DD/YYYY (disambiguated when one
  segment exceeds 12), and DD-Mon-YYYY. This fallback lives entirely in
  the new template file, not in the shared helper, so no other existing
  template's date parsing is affected.
- **Amount**: the shared `_parse_decimal` helper; rejected if it parses
  to `None` or is not strictly positive.
- **Debit/Credit**: DEBIT/CREDIT/DR/CR/D/C all normalize correctly;
  anything else is rejected (`INVALID_DEBIT_CREDIT`) - never silently
  guessed.
- **Currency**: validated against the existing `Currency` table; no
  second currency table introduced.
- **Narration/references**: preserved verbatim, never rewritten or
  AI-cleaned.

## 7. Duplicate detection

Two categories, both required:

**A. Within the same file**: the generic engine's own `duplicate_key`
hook (already used by every other template) - built from raw row text
(bank account, date, entry type, amount, currency, reference fields,
and narration only when no reference is present). A collision is a
`DUPLICATE_ROW` warning; the row is excluded from import.

**B. Against previously imported evidence**: a query inside `validate_row`
against `BankStatementTransaction.duplicate_key` - this is the piece the
generic engine cannot see on its own (it only tracks keys within the
current file). The deterministic key is built from `bank_account_id`,
`transaction_date`, `entry_type`, `amount`, `currency_code`,
`bank_reference`, `external_transaction_id`, and - **only when neither
reference field is present** - `narration`.

**`has_strong_identity`** (true when a bank reference or external
transaction ID is present) determines the classification of a
collision:
- Strong identity + collision → `DUPLICATE_TRANSACTION` (ERROR, not
  imported) - backed at the database level by
  `ux_bank_statement_transactions_strong_duplicate_key`, a partial
  unique index on `duplicate_key WHERE has_strong_identity = true`.
- Weak identity + collision → `POTENTIAL_DUPLICATE` (WARNING, still
  imported) - the database does NOT enforce uniqueness here, since two
  such rows may legitimately be different transactions.

**Never "same date + amount = duplicate"**: verified explicitly by
`test_never_uses_same_date_and_amount_alone_as_duplicate_rule` - two
rows with the same account/date/amount/currency/direction but different
narration and no reference both import cleanly, with zero duplicate
flag, because narration differs.

## 8. Statement period overlap / reimport protection

`app/services/bank_statement_service.py::annotate_statement_period_overlap`
runs additively (called from the upload endpoint, never inside the
shared engine) after validation. For each bank account referenced in the
uploaded file, it checks whether any *other* import batch already holds
`BankStatementTransaction` rows with an overlapping
`statement_period_start`/`statement_period_end` range, and if so records
a batch-level (`row_number=0`) `STATEMENT_PERIOD_OVERLAP` **warning** -
never a rejection, since a legitimate corrected/reissued statement can
genuinely cover the same period twice. Individual overlapping
transactions are still separately caught (or not) by the exact/potential
duplicate detection above - the two mechanisms are independent and
complementary. Verified by
`test_overlapping_statement_period_is_flagged_but_not_rejected`.

## 9. Entity and RBAC security

Every endpoint uses the existing `app.auth.authorization` module - no
new authorization mechanism. `TreasuryModule.BANK_RECONCILIATION`
(already reserved since Stage 0) gates the new `/bank-statements/*`
viewer endpoints; the existing `TreasuryModule.EXCEL_DATA_HUB` continues
to gate upload/confirm, exactly as it already does for every other
template.

Verified: an entity-scoped user cannot upload into another entity
(403), cannot read another entity's import batch or preview (403),
cannot list or fetch another entity's statement transactions by filter
or direct ID (403, and never present in an unfiltered list either), and
- critically - **cannot reference another entity's real bank account
number inside a file that otherwise claims to belong to their own
entity** (`BANK_ACCOUNT_NOT_AUTHORIZED`, caught server-side regardless
of what the uploaded file claims). Cross-group access is denied the same
way. See `tests/test_bank_statement_security_and_concurrency.py`.

## 10. Multi-currency

`BankStatementTransaction.currency_code` preserves the statement's own
transaction currency exactly - never converted to reporting currency,
never merged across currencies. Verified with an NGN and a USD account
imported in the same file, both correctly retained in their own
currency (`test_multi_currency_preserved_original_currency_never_converted`).
No FX matching logic exists in Stage 5A - that is explicitly deferred to
whichever later sub-stage implements FX-aware matching.

## 11. Corrected finding: no Tasks/Workflow system exists yet

The Stage 5 forensic report already identified this: Treasury OS has no
`Task` model, table, service, or API - `TreasuryModule.TASKS_WORKFLOW`
is an unbuilt Stage-0 RBAC placeholder, and the roadmap schedules real
Tasks & Workflow for Stage 7. This has no bearing on Stage 5A itself
(Stage 5A creates no open items and assigns nothing to anyone), but is
restated here for documentation completeness since the original Stage
5A brief referenced RBAC/workflow concepts broadly.

## 12. API endpoints

Reused (zero changes to endpoint shape, only to internal locking):
`POST /excel/uploads`, `GET /excel/validation/{batch_id}`,
`POST /excel/imports/{batch_id}/confirm`, `GET /excel/imports`,
`GET /excel/templates`.

New (`app/api/v1/bank_statements.py`):
`GET /bank-statements/transactions` (entity/group-scoped list, optional
`bank_account_id`/`import_batch_id` filters), `GET
/bank-statements/transactions/{id}` (detail, entity-scoped).

## 13. Frontend

The existing Excel Data Hub page (`frontend/app/excel-data-hub/page.tsx`)
is fully generic - it lists templates from `GET /excel/templates` and
already supports `BANK_STATEMENT` with **zero frontend changes**, since
the template auto-seeds into the registry. One new page was added,
`frontend/app/bank-statements/page.tsx` - a read-only evidence viewer
(SECTION 24's "Statement transaction view" requirement), using Treasury
OS's existing design language, no reconciliation UI of any kind.

## 14. Known limitations (deferred to later sub-stages)

- No matching, candidate generation, or scoring of any kind (Stage 5C).
- No `reconciliation_open_items`, assignment, aging, or resolution
  workflow (Stage 5D/5E).
- No reconciliation reports (Stage 5F).
- No adaptive learning (Stage 5G).
- No dedicated reconciliation frontend (Stage 5H).
- Mapping-preset convenience (saved column-mapping per bank/account,
  present in the old bank-matcher app the forensic report reviewed) was
  not built - deferred, low-risk, not required for a first cut.
- A future correction/reversal workflow for
  `BankStatementTransactionStatus.REVERSED` is not implemented - the
  enum value is reserved but no code path sets it.
