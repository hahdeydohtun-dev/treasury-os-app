# Excel Data Hub

The Excel Data Hub is the first working ingestion adapter for the Treasury
Data Model (see `TREASURY_DATA_MODEL.md`). This document covers the
workflow, the template registry, validation rules, duplicate detection,
and how to add a new template.

## Workflow

```
SELECT TEMPLATE
      v
UPLOAD EXCEL          POST /api/v1/excel/uploads  (multipart: template_code,
                                                    template_version, file,
                                                    optional legal_entity_id)
      v
READ FILE             openpyxl parses the first worksheet; row 1 = header
      v
VALIDATE STRUCTURE    every required column from the template must be present,
                      or the batch fails outright (status FAILED)
      v
VALIDATE DATA         each row runs through the template's row validator:
                      required fields, data types, dates, amounts, currency
                      codes, entity/bank/account existence, debit/credit
                      indicators, FX rate types
      v
CHECK DUPLICATES      each row's business key is compared against every
                      other row already seen in this file
      v
SHOW PREVIEW          GET /api/v1/excel/validation/{batch_id} -
                      total/valid/invalid/warning/duplicate counts + the
                      full per-row issue list
      v
USER CONFIRMS IMPORT  POST /api/v1/excel/imports/{batch_id}/confirm
      v
IMPORT TO POSTGRESQL  every row with no ERROR-level issue and no duplicate
                      flag is written to its domain table
      v
IMPORT SUMMARY        updated ImportBatch: imported_rows, skipped_rows,
                      final status
```

Nothing lands in a domain table (BankAccount, BankBalance,
TreasuryTransaction, ExpectedCollection, ExpectedPayment, FXRate,
BankCharge) until the confirm step. Up to that point, the parsed rows live
in ImportBatch.raw_rows (JSON) and the validation results in
ImportIssue rows - so the preview is guaranteed to reflect exactly what
will happen on import, because confirm_import re-runs the same
validators over the same staged rows.

## Import Batch lifecycle

| Status | Meaning |
|---|---|
| UPLOADED | Transient - set then immediately moved past during upload handling |
| VALIDATING | Transient - validators are running |
| VALIDATED | Structure and data validated, but zero valid rows to import |
| READY_FOR_IMPORT | At least one valid, non-duplicate row - awaiting confirm |
| IMPORTING | Transient - import in progress |
| IMPORTED | Every valid row imported, nothing skipped |
| PARTIALLY_IMPORTED | Some rows imported, some skipped (invalid/duplicate) |
| FAILED | Missing required column(s), or zero rows actually imported |
| CANCELLED | Reserved for a future explicit cancel action (not yet exposed) |

Every batch records: template + version, uploader, upload time, source
entity (if scoped), and total/valid/invalid/warning/imported/duplicate row
counts - satisfying the audit-trail requirement (SECTION 16) without a
separate audit table, since ImportBatch itself is the record, and every
create/upload/import action additionally writes an AuditEvent (module
EXCEL_DATA_HUB).

## Supported templates

| Code | Purpose | Required columns |
|---|---|---|
| BANK_ACCOUNTS | Create bank accounts in bulk | Entity, Bank, Account Name, Account Number, Currency |
| BANK_BALANCES | Import daily balances | Entity, Bank, Account Number, Balance Date, Currency, Closing Balance |
| BANK_TRANSACTIONS | Import posted bank transactions | Entity, Bank, Account Number, Transaction Date, Currency, Amount, Debit/Credit |
| EXPECTED_COLLECTIONS | Forecast input: expected receipts | Entity, Expected Date, Currency, Amount |
| EXPECTED_PAYMENTS | Forecast input: expected payments | Entity, Expected Date, Currency, Amount |
| FX_RATES | Bulk FX rate updates | Rate Date, From Currency, To Currency, Rate |
| BANK_CHARGES | Bank fees/charges | Entity, Bank, Account Number, Charge Date, Currency, Amount |

Full required/optional column lists are returned by
GET /api/v1/excel/templates and defined in
app/services/excel_templates.py::TEMPLATE_REGISTRY.

### Template versioning

Every template has a code, name, version, and required/optional
column lists (ExcelTemplate DB rows, seeded by
app/db/seed_reference_data.py to mirror the registry). An upload
specifying a (template_code, template_version) pair that doesn't match
the current registry version is rejected before parsing
(TemplateVersionError -> HTTP 400). Bumping a template's required
columns means bumping TemplateSpec.version in the registry and adding a
new ExcelTemplate row - old uploads referencing the old version are
simply no longer accepted, rather than silently validated against the
wrong rules.

## Validation rules

Each template's row validator (app/services/excel_templates.py) checks,
as relevant to that template: required fields present, dates parseable,
amounts numeric, currency codes exist in Currency, referenced entities/
banks/bank accounts exist, debit/credit indicators recognized, FX rate
types recognized. Every issue records row number, column name, the
offending value, an error code (e.g. INVALID_CURRENCY,
ACCOUNT_NOT_FOUND, REQUIRED_FIELD), and a human-readable message -
matching the SECTION 14 example format exactly (row/column/value/error
code/message).

Errors vs. warnings: a row with any ERROR-level issue is excluded from
import. A row with only WARNING-level issues (e.g. MISSING_REFERENCE on
a bank transaction with no reference at all) is still imported - warnings
flag something worth a human's attention without blocking the import.

## Duplicate detection

Each template defines a business key function (duplicate_key on
TemplateSpec) rather than relying on Excel row numbers, per SECTION 15:

| Template | Business key |
|---|---|
| Bank Accounts | (bank, account number) |
| Bank Balances | (account number, balance date) |
| Bank Transactions | (entity, account number, value/transaction date, amount, reference) |
| Expected Collections/Payments | (entity, expected date, amount, reference) |
| FX Rates | (from currency, to currency, rate type, rate date) - see note below |
| Bank Charges | (account number, charge date, amount, reference) |

Duplicate detection is currently intra-batch: if two rows in the same
upload share a business key, the second is flagged DUPLICATE_ROW
(a warning) and excluded from import. This satisfies "never silently
duplicate financial transactions" for the common case (the same file
uploaded twice, or containing accidental repeats) without conflicting with
legitimate re-imports across separate uploads for models that are meant
to be corrected over time.

FX Rates are a deliberate exception: two different uploads for the
same (from, to, rate_type, rate_date) are not duplicates - they're
corrections, and each one creates a new FXRate.version via the same
versioning path as the original FXRate API (POST /fx-rates), never overwriting
the prior row. Intra-batch duplicates (the same identity appearing twice
in one file) are still flagged and only the first is imported.

BankBalance additionally has a database-level uniqueness constraint on
(bank_account_id, balance_date, source), so a cross-batch duplicate
(re-uploading the same day's balance from a different file) is rejected
with a 409 if attempted through the direct API, though the Excel import
path currently only de-duplicates within a batch - re-uploading the same
balances file twice would attempt a second insert and hit that database
constraint, which the import path does not yet catch gracefully. This is
a known gap to close if the Cash & Liquidity module needs it.

## Authorization

Upload and confirm-import both require the caller to hold
EXCEL_DATA_HUB permission for the target entity - UPLOAD for
uploading, IMPORT for confirming. Because the target entity comes from
the request body (a legal_entity_id form field) rather than the URL, the
generic require_permission dependency (which only reads path/query
params) is not sufficient on its own; these endpoints authenticate via
get_current_user and check _grants_permission(...) explicitly against
the body's entity id. A user scoped to Entity A cannot upload or confirm
an import targeting Entity B (tested in
tests/test_excel_data_hub.py::test_entity_scoped_user_cannot_upload_for_other_entity).
See ARCHITECTURE.md section 12.2 for the full authorization design note.

## Data freshness

GET /api/v1/excel/freshness reports, per template, the last successful
(IMPORTED or PARTIALLY_IMPORTED) batch's upload time, status, imported row
count, age in hours, and a CURRENT/STALE/NO_DATA status against a
configurable threshold (stale_after_hours, default 24). This is
per-template rather than per-source-system, since a template maps
cleanly onto "a kind of data" (bank balances, FX rates, ...) regardless of
which bank or file it came from.

## Adding a new template

1. Add a TemplateSpec entry to
   app/services/excel_templates.py::TEMPLATE_REGISTRY: required/optional
   columns, a row validator (async def(row, db) -> list[RowIssue]), a
   duplicate key function (def(row) -> tuple), and an importer
   (async def(row, db, batch_id, entity_scope) -> domain_object).
2. Add a matching ExcelTemplate seed row (or add it to
   app/db/seed_reference_data.py) so GET /excel/templates lists it.
3. Nothing else changes - app/services/excel_service.py and
   app/api/v1/excel.py are entirely generic over the registry.
