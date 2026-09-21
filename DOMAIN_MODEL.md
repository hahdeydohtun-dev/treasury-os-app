# Treasury OS — Domain Model (Foundation Stage)

This documents the tables that exist today. Future modules will add tables
that reference these via `legal_entity_id` / `group_id` /
`from_currency_code` / `to_currency_code` foreign keys, following the same
mixins and conventions.

## Shared conventions

Every table inherits from `Base` (`app/db/base_class.py`) plus a subset of:

- **`UUIDPKMixin`** — `id: UUID`, app-generated (`uuid.uuid4()`), not a DB
  sequence. Chosen so IDs are stable and generatable before insert.
- **`TimestampMixin`** — `created_at`, `updated_at`, both DB-managed
  (`server_default=func.now()`), never set by application code.
- **`SoftDeleteMixin`** — `is_active: bool`, `deactivated_at: datetime |
  None`, plus a `.deactivate()` helper. Used on anything with financial or
  audit significance, per Principle 5 (never silently overwrite/destroy
  history). `AuditEvent` and `FXRate` intentionally do **not** use this
  mixin — they're append-only by design, so "soft delete" doesn't apply.

## Entities

### `Group`
Top-level consolidation unit.

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| name | string, unique | |
| code | string, unique | short code |
| reporting_currency_code | FK → `currencies.code` | group consolidation currency |
| description | string, nullable | |
| is_active / deactivated_at | soft delete | |
| created_at / updated_at | timestamps | |

Relationship: `Group.legal_entities` (one-to-many, cascade delete-orphan).

### `LegalEntity`
Belongs to exactly one `Group`. The anchor point for every financial
object per Principle 1.

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| group_id | FK → `groups.id` | |
| name | string | |
| code | string, unique | |
| country | string(2), nullable | ISO 3166-1 alpha-2 |
| functional_currency_code | FK → `currencies.code` | |
| registration_number | string, nullable | |
| tax_id | string, nullable | |
| is_active / deactivated_at | soft delete | |
| created_at / updated_at | timestamps | |

### `BusinessUnit`
Optional sub-division of a `LegalEntity`. Architecture-ready, not used by
any current feature.

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| legal_entity_id | FK → `legal_entities.id` | |
| name | string | |
| code | string | |
| is_active / deactivated_at | soft delete | |
| created_at / updated_at | timestamps | |

## Currency & FX

### `Currency`
Master/configuration data — not hard-coded, not an enum.

| column | type | notes |
|---|---|---|
| code | string(3) PK | ISO 4217, e.g. `USD` |
| name | string | |
| symbol | string(5), nullable | |
| decimal_places | int, default 2 | |
| is_base_currency | bool | usable as a Group reporting currency |
| is_active / deactivated_at | soft delete | |
| created_at / updated_at | timestamps | |

### `FXRate`
Effective-dated, versioned, append-only. See ARCHITECTURE.md §4 for the
versioning rule.

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| from_currency_code | FK → `currencies.code` | |
| to_currency_code | FK → `currencies.code` | |
| rate_type | enum: SPOT/BUDGET/MANAGEMENT/TREASURY/HISTORICAL | |
| rate_date | date | |
| rate | numeric(20,10) | |
| rate_source | string, default `MANUAL` | |
| version | int, default 1 | increments per correction |
| is_current | bool | true only for the latest version of an identity |
| superseded_by_id | FK → `fx_rates.id`, nullable | set on the old row when superseded |
| created_by_user_id | FK → `users.id`, nullable | |
| created_at / updated_at | timestamps | |

Unique constraint: `(from_currency_code, to_currency_code, rate_type,
rate_date, version)`.

## RBAC

### `User`

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| email | string, unique | |
| full_name | string | |
| hashed_password | string | bcrypt via passlib |
| is_superuser | bool | bypasses all scoping |
| mfa_enabled | bool | MFA-ready; TOTP verification not implemented |
| mfa_secret | string, nullable | |
| is_active / deactivated_at | soft delete | |
| created_at / updated_at | timestamps | |

### `Role`

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| name | string, unique | |
| description | string, nullable | |
| is_system_role | bool | seeded roles vs. user-created |
| is_active / deactivated_at | soft delete | |

Relationship: `Role.permissions` (one-to-many, cascade delete-orphan).

### `Permission`
A single (module, action) grant belonging to a `Role`.

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| role_id | FK → `roles.id` | |
| module | enum `TreasuryModule` | one value per SECTION 4 module |
| action | enum `TreasuryAction` | VIEW/UPLOAD/IMPORT/CREATE/EDIT/SUBMIT/INVESTIGATE/ASSIGN/COMMENT/APPROVE/EXECUTE/RESOLVE/CLOSE/ADJUST/EXPORT/CONFIGURE |

Unique constraint: `(role_id, module, action)`.

### `UserRoleAssignment`
Binds a `User` + `Role` to a scope.

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| user_id | FK → `users.id` | |
| role_id | FK → `roles.id` | |
| scope_type | enum: `GROUP_WIDE` \| `ENTITY` | |
| legal_entity_id | FK → `legal_entities.id`, nullable | required when scope_type=ENTITY |
| group_id | FK → `groups.id`, nullable | |
| is_active / deactivated_at | soft delete | |

## Audit

### `AuditEvent`
Append-only. No `SoftDeleteMixin` — rows are never deactivated or deleted.

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| user_id | FK → `users.id`, nullable | who |
| action | string | what (e.g. `CREATE`) |
| module | string, indexed | e.g. `CURRENCY_FX` |
| group_id | UUID, nullable | |
| legal_entity_id | UUID, nullable, indexed | |
| record_type | string | e.g. `FXRate` |
| record_id | string, indexed | |
| previous_value | JSONB, nullable | |
| new_value | JSONB, nullable | |
| reason | string, nullable | |
| approval_reference | string, nullable | |
| source | string, default `API` | |
| ip_address | string, nullable | |
| session_id | string, nullable | |
| created_at | timestamp | acts as the event time |

## Entity-relationship summary

```
Group 1───* LegalEntity 1───* BusinessUnit
  │
  └── reporting_currency_code ──► Currency ◄── functional_currency_code ── LegalEntity

Currency ◄── from_currency_code / to_currency_code ── FXRate ──► FXRate (superseded_by_id, self-referencing)

User 1───* UserRoleAssignment *───1 Role 1───* Permission
UserRoleAssignment ──► LegalEntity (nullable, when ENTITY-scoped)
UserRoleAssignment ──► Group (nullable)

AuditEvent ──► User (nullable, who performed the action)
```

## Stage 2 tables

Full column-level detail for these lives in `TREASURY_DATA_MODEL.md`
(domain tables) and `EXCEL_DATA_HUB.md` (ingestion tables). Summary:

| Table | Purpose |
|---|---|
| `account_types` | Configurable bank account type lookup (Current, Savings, Escrow, ...) |
| `cash_event_types` | Configurable cash event type lookup (BANK_RECEIPT, SUPPLIER_PAYMENT, ...), each with a default INFLOW/OUTFLOW/TRANSFER/NON_CASH direction |
| `banks` | Group-level shared bank master data |
| `bank_accounts` | Entity + bank + currency + account-type scoped bank account |
| `bank_balances` | Imported opening/closing/available/ledger balances; unique per (account, date, source) |
| `treasury_transactions` | The common financial data layer / "cash event" model every future module feeds |
| `expected_collections` | Forward-looking expected customer collections (forecast input) |
| `expected_payments` | Forward-looking expected supplier/other payments (forecast input) |
| `bank_charges` | Incidental bank fees/charges |
| `excel_templates` | Versioned Excel template registry |
| `import_batches` | One row per upload; carries `raw_rows` (staged, pre-import) and lifecycle status |
| `import_issues` | Per-row/column validation errors and warnings for an import batch |

Stage 1's entity-relationship additions:

```
LegalEntity 1───* BankAccount *───1 Bank
BankAccount 1───* BankBalance
LegalEntity 1───* TreasuryTransaction ──► BankAccount (nullable), Bank (nullable)
TreasuryTransaction ──► CashEventType (event_type_code)
LegalEntity 1───* ExpectedCollection
LegalEntity 1───* ExpectedPayment
LegalEntity 1───* BankCharge ──► BankAccount

ImportBatch 1───* ImportIssue
ImportBatch ──► (BankAccount | BankBalance | TreasuryTransaction | ExpectedCollection |
                 ExpectedPayment | FXRate | BankCharge)   via import_batch_id on each,
                 i.e. every imported row traces back to the batch that created it
```

## Stage 2 tables

Full detail in `FORECAST_ENGINE.md` / `FORECAST_METHODOLOGY.md`. Summary:

| Table | Purpose |
|---|---|
| `forecast_categories` | Configurable cash-flow category hierarchy (INFLOW/OUTFLOW/TRANSFER/NON_CASH) |
| `forecasts` | One versioned forecast run — scenario, value basis, reporting currency, status, parent version link |
| `forecast_weeks` | Consolidated (reporting-currency) weekly waterfall: opening/closing cash, inflows/outflows/transfers, minimum liquidity, surplus/gap, coverage ratio |
| `forecast_lines` | Every individual cash-flow driver: entity, category, currency, original + adjusted + reporting amounts, FX rate used, source type/id |
| `forecast_scenario_assumptions` | Configurable BASE/CONSERVATIVE/STRESS parameters attached to a forecast |
| `forecast_adjustments` | Manual, additive, auditable forecast overrides (never modify source data) |
| `recurring_cash_flows` | Recurring forecast assumptions (payroll, rent, ...) — never generate actual transactions |
| `liquidity_thresholds` | Configurable minimum cash requirements by group/entity/currency/bank-account scope |
| `forecast_alerts` | Liquidity-gap, low-coverage, and currency-shortfall alerts generated at calculation time |

Stage 2's entity-relationship additions:

```
Group 1───* Forecast (legal_entity_id nullable = group-wide)
Forecast 1───* ForecastWeek
Forecast 1───* ForecastLine ──► ForecastWeek, LegalEntity, ForecastCategory
Forecast 1───* ForecastScenarioAssumption
Forecast 1───* ForecastAdjustment
Forecast 1───* ForecastAlert
Forecast ──► Forecast (parent_forecast_id, self-referencing rolling-version chain)

LegalEntity 1───* RecurringCashFlow
LiquidityThreshold ──► LegalEntity (nullable), BankAccount (nullable)

ForecastLine ──► (ExpectedCollection | ExpectedPayment | RecurringCashFlow |
                   TreasuryTransaction | ForecastAdjustment)  via source_type + source_id,
                  i.e. every forecast amount traces back to the record that produced it
```
