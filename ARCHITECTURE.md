# Treasury OS — Architecture

This document describes the architecture established at the foundation
stage. It is written to be extended, not rewritten, as each treasury module
is built.

## 1. Guiding principles (from the product spec)

These are enforced structurally, not just by convention:

1. **Every financial object belongs to a legal entity.** `LegalEntity` is a
   first-class table; every future domain model (payments, facilities,
   investments, etc.) will carry a `legal_entity_id` foreign key.
2. **Every relevant financial object has a currency.** `Currency` is
   configuration data (see `app/models/currency.py`), never hard-coded.
3. **Every important financial object has a lifecycle.** Enforced per
   module as it's built (e.g. facility lifecycle events, investment
   lifecycle events) — not yet applicable to the foundation-stage models.
4. **Every material financial change has an audit trail.** `AuditEvent` is
   the single append-only table for this; see `app/services/audit_service.py`.
5. **Historical financial information is never silently overwritten.**
   Enforced today via `FXRate.version` / `is_current` / `superseded_by_id`
   (see `POST /api/v1/fx-rates`, which always inserts a new version rather
   than updating in place) and via `SoftDeleteMixin` (deactivate, don't
   delete).
6. **The database is the source of truth.** All computed/derived data will
   be computed by the (future) Treasury Engine and persisted, not held only
   in application memory or recomputed ad hoc by the frontend.
7. **Excel is an external data-ingestion mechanism only.** Not yet built;
   the API layer (`/api/v1/*`) is structured so an Excel Data Hub service
   would write through the same persistence layer as any future direct API
   integration (principle 8).
8. **Future APIs must be able to replace Excel ingestion without changing
   core business logic.** Achieved by keeping ingestion (whatever the
   source) as a thin adapter that produces the same domain records; no
   business logic should ever live inside an import/parsing step.
9. **Deterministic financial calculations are performed by the Treasury
   Engine, not AI.** Not yet built; when it is, calculation code will live
   in `app/services/*` and be fully unit-testable independent of any LLM
   call.
10. **AI may analyze/explain/recommend; material actions require human
    authorization.** The (future) AI Copilot will call read-only services
    and, for anything that writes data, go through the same
    `require_permission` + audit path as a human-initiated request — there
    is no separate "AI bypass" in the authorization model.

## 2. Repository layout

```
treasury-os/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app entrypoint
│   │   ├── core/                # config, security (hashing/JWT)
│   │   ├── db/                  # engine/session, declarative base + mixins
│   │   ├── auth/                # current-user + permission dependencies
│   │   ├── models/               # SQLAlchemy ORM models (one file per domain area)
│   │   ├── schemas/              # Pydantic request/response schemas
│   │   ├── services/             # business logic (audit_service today; treasury
│   │   │                         #   engine services will live here)
│   │   └── api/v1/               # versioned REST routers
│   ├── alembic/                  # migrations
│   ├── tests/                    # pytest suite
│   ├── requirements.txt
│   └── alembic.ini
├── frontend/
│   ├── app/                       # Next.js App Router pages
│   ├── components/                # shared React components (Sidebar, etc.)
│   ├── lib/                        # API client, nav config
│   └── package.json
├── docker-compose.yml              # Postgres + Redis for local dev
├── .env.example
├── README.md
├── ARCHITECTURE.md                 # (this file)
├── DOMAIN_MODEL.md
└── DEVELOPMENT_ROADMAP.md
```

## 3. Multi-entity architecture

```
Group
 └── LegalEntity  (functional_currency_code, country, ...)
      └── BusinessUnit  (architecture-ready, optional)
```

- `Group` is the top-level consolidation unit and carries the group's
  reporting currency.
- `LegalEntity` belongs to exactly one `Group` and carries its own
  functional currency — this is the anchor point every future financial
  object (bank account, transaction, facility, investment, etc.) will
  attach to via `legal_entity_id`.
- `BusinessUnit` is modeled now (optional sub-division of a `LegalEntity`)
  so a later cost-center-level reporting requirement doesn't force a
  breaking schema migration, even though nothing currently uses it.

Drill-down (`Group → Entity → Currency → Bank → Bank Account → Transaction`)
is a query-composition concern, not a separate schema concept: each level
already carries the foreign keys needed to filter down from any parent.

## 4. Multi-currency architecture

`Currency` (see `app/models/currency.py`) is a master table keyed by ISO
4217 code (e.g. `USD`), not an enum — new currencies are configuration,
added via `POST /api/v1/currencies`, never a code change.

`FXRate` is the effective-dated, versioned, auditable rate table:

- Identity = `(from_currency_code, to_currency_code, rate_type, rate_date)`.
- Each correction creates a **new row** with `version = previous + 1`, and
  marks the previous row `is_current = False` with `superseded_by_id`
  pointing at the new row. The original row and its value are never
  mutated. This is enforced in `app/api/v1/currencies.py::create_fx_rate`
  and covered by `tests/test_fx_rate_versioning.py`.
- `rate_type` supports SPOT / BUDGET / MANAGEMENT / TREASURY / HISTORICAL
  today, extensible via the `FXRateType` enum.

Every future financial transaction table is expected to carry the six-field
shape from the spec: `transaction_currency/amount`,
`functional_currency/amount`, `reporting_currency/amount`, plus
`exchange_rate`, `rate_date`, `rate_type`, `rate_source` — this hasn't been
built yet since no transaction-bearing module exists yet, but the `FXRate`
table is what those fields will resolve against.

## 5. Authorization architecture (RBAC)

Model: **User → UserRoleAssignment → Role → Permission(module, action)**,
with the assignment carrying the scope.

- `TreasuryModule` and `TreasuryAction` are enums (see `app/models/rbac.py`)
  — one value per module from the product spec, and one value per action
  type (VIEW, CREATE, EDIT, APPROVE, ...). Adding a new module or action to
  support a future feature is an enum addition, not a new table.
- `UserRoleAssignment.scope_type` is either `GROUP_WIDE` (applies across
  every legal entity in the group) or `ENTITY` (applies only to the one
  `legal_entity_id` on the assignment row). A user can hold multiple
  assignments — e.g. group-wide `VIEW` on Reconciliation plus
  entity-scoped `APPROVE` on Entity A only.
- Enforcement happens via the `require_permission(module, action)`
  dependency factory (`app/auth/dependencies.py`), used per-endpoint:

  ```python
  @router.get("/reconciliation/{entity_id}/items")
  async def list_open_items(
      entity_id: uuid.UUID,
      user: User = Depends(require_permission(
          TreasuryModule.BANK_RECONCILIATION, TreasuryAction.VIEW
      )),
  ):
      ...
  ```

  The dependency resolves `entity_id` from the request's path/query params,
  then checks whether any of the user's active role assignments grant that
  (module, action) either group-wide or for that specific entity. It fails
  closed: an entity-scoped grant with no resolvable `entity_id` in the
  request does not fall back to "allow".
- `User.is_superuser` bypasses scoping entirely (for local/dev bootstrap
  and true system administrators) — this is a deliberate, narrow escape
  hatch, not the default path.
- Value limits, approval thresholds, and segregation-of-duties (mentioned
  in the spec as future work) are not implemented yet; the `Permission`
  table's `(role_id, module, action)` uniqueness constraint is the natural
  place to attach a `value_limit` column later without restructuring the
  model.

## 6. Audit architecture

`AuditEvent` is a single, append-only table capturing who / what / when /
entity / module / record / previous+new value / reason / approval
reference / source / IP-session. Nothing writes to it directly — all
application code goes through
`app.services.audit_service.record_audit_event(...)`, called inside the
same DB transaction as the change it's recording (see
`app/api/v1/entities.py` and `app/api/v1/currencies.py` for the pattern).
This keeps the audit shape consistent as more modules are added, and makes
"traceable financial history" (spec requirement) a property of the write
path rather than something each module has to remember to implement.

`GET /api/v1/audit` is a read-only, permission-gated
(`TreasuryModule.AUDIT`, `TreasuryAction.VIEW`) query endpoint with filters
on module / record_type / record_id.

## 7. Deterministic engine vs. AI

No Treasury Engine or AI Copilot exists yet at this stage. The separation
is nonetheless baked into the architecture:

- `app/services/` is where deterministic calculation logic will live
  (balances, forecasts, interest, schedules, reconciliation matching,
  KPIs, variances) — plain Python/SQL, fully unit-testable, no LLM calls.
- The (future) AI Copilot will be a separate service module that *calls
  into* the same deterministic services and the same `require_permission`
  + `record_audit_event` path as any other write — it will not have a
  privileged bypass. This is a structural constraint documented here so
  it's honored when that module is built, not an aspiration.

## 8. API design

All routes are versioned under `/api/v1` (`app/main.py`,
`app/core/config.py::API_V1_PREFIX`). Each domain area is its own router
file under `app/api/v1/`, aggregated in `app/api/v1/router.py`. This means
a new module (e.g. Payments) adds a new router file and one line in
`router.py` — it does not touch existing routers.

## 9. Frontend architecture

Next.js App Router, TypeScript. `lib/nav-items.ts` is the single source of
truth for the left-hand navigation and which modules are enabled; a module
route only renders when a real page exists, so half-built modules aren't
silently reachable. `lib/api-client.ts` is a minimal typed fetch wrapper
around the FastAPI backend — no heavier data-fetching library has been
introduced yet at this stage.

## 10. Testing strategy

- `tests/test_security.py` — password hashing / JWT round-trip (unit).
- `tests/test_rbac.py` — the `_grants_permission` scoping function tested
  directly against constructed in-memory objects (business-rule unit
  tests, no DB needed).
- `tests/test_auth_api.py`, `tests/test_fx_rate_versioning.py` — integration
  tests against a real Postgres instance via httpx `ASGITransport`, with an
  autouse fixture that truncates the relevant tables before each test
  (`tests/conftest.py::_clean_database`) since assertions rely on the DB
  actually persisting data, not a mocked session.
- `tests/test_health.py` — smoke test that the app boots and serves.

## 11. What's deliberately deferred

Anything described in spec sections 7–13 and 16 as a "core module" (Cash &
Liquidity Engine, 13-Week Forecast, Facilities, Investments, Bank/
Intercompany Reconciliation, Excel Data Hub) is **not implemented** at this
stage per the explicit foundation-first instruction. The schema and API
conventions above are designed so each can be added as an independent
vertical slice (model file + schema file + router file + service file)
without modifying the foundation.

## 12. Stage 1 — Treasury Data Foundation + Excel Data Hub

Stage 1 adds the common financial data layer every future module (payments,
loans, investments, intercompany, working capital) will feed through,
rather than each module inventing its own incompatible transaction table.
See `TREASURY_DATA_MODEL.md` and `EXCEL_DATA_HUB.md` for the full detail;
this section summarizes how it fits the existing architecture.

### 12.1 What was added

- **Configurable lookups** (`AccountType`, `CashEventType`): follow the same
  pattern as `Currency` — a natural code as primary key, seeded with
  sensible defaults, extendable via the API without a schema change. Never
  a fixed Python enum baked into business logic.
- **Bank & BankAccount**: `Bank` is group-level shared master data (a real
  institution can serve multiple entities); `BankAccount` is
  entity-scoped, currency-scoped, and account-type-scoped, per PRINCIPLE 1.
- **TreasuryTransaction**: the generic "cash event" model. Every event
  carries the full multi-currency shape established for `FXRate`
  (transaction/functional/reporting amount + currency, plus the FX rate
  fields), classifies itself as INFLOW/OUTFLOW/TRANSFER/NON_CASH, and
  carries full source traceability (`source_type`, `source_file`,
  `source_record_id`, `import_batch_id`). Module-specific detail (a future
  Loan's amortization schedule, for example) belongs in its own table that
  references a TreasuryTransaction for its cash-flow leg — not folded into
  this table.
- **ExpectedCollection / ExpectedPayment / BankCharge**: forward-looking
  and incidental cash-flow inputs, kept separate from actual/posted
  transactions since they have a different lifecycle (probability, status)
  and are explicit future-forecast inputs (SECTION 29).
- **BankBalance**: imported bank balances, one row per
  (account, date, source) with a uniqueness constraint preventing
  accidental duplicate imports.
- **Excel Data Hub** (`ExcelTemplate`, `ImportBatch`, `ImportIssue`): the
  full upload → validate → preview → confirm → import lifecycle. See
  `EXCEL_DATA_HUB.md`.
- **Cash Position service**: reads the latest `BankBalance` per account
  and aggregates by group/entity/currency/bank/account — the foundation
  for the future Daily Cash Position Engine, not the engine itself.

### 12.2 Authorization lesson learned

Several Stage 1 write endpoints take the target `legal_entity_id` from the
request **body** (e.g. creating a bank account, uploading an Excel file)
rather than the URL. The shared `require_permission` dependency only
resolves `entity_id` from path/query parameters, so using it directly as
the sole guard on these endpoints would silently require a `GROUP_WIDE`
grant and reject legitimately-scoped entity users. The fix, applied
consistently: these endpoints authenticate via `get_current_user` and then
call `_grants_permission(...)` explicitly against the body's entity id.
`require_permission` was also extended to recognize `legal_entity_id` (not
just `entity_id`) as a query parameter name, since that's the naming
convention Stage 1's list endpoints use.

### 12.3 Excel ingestion architecture

```
EXCEL UPLOAD (.xlsx)
      ↓  openpyxl parses the first worksheet
READ FILE → rows staged as JSON on ImportBatch.raw_rows
      ↓
VALIDATE STRUCTURE  → missing required columns => batch FAILS outright
      ↓
VALIDATE DATA       → per-template row validator (references, types, FKs)
      ↓
CHECK DUPLICATES    → per-template business key, intra-batch
      ↓
PREVIEW             → ImportBatch counts + ImportIssue rows, via API
      ↓
USER CONFIRMS       → POST /excel/imports/{id}/confirm
      ↓
IMPORT TO POSTGRESQL → per-template importer creates the real domain rows
      ↓
IMPORT SUMMARY       → updated ImportBatch counts/status
```

Nothing lands in a domain table (BankAccount, BankBalance,
TreasuryTransaction, etc.) until the confirm step runs — `raw_rows` +
`ImportIssue` fully describe the preview. Adding a new template means
adding one `TemplateSpec` entry in `app/services/excel_templates.py`
(required/optional columns, a row validator, a duplicate key function, and
an importer) — the upload/validate/confirm endpoints in
`app/api/v1/excel.py` are entirely generic over the registry.

FX Rates uploaded via Excel reuse the exact same versioning path as the
the original `POST /fx-rates` endpoint: a corrected rate creates a new
`FXRate` row with an incremented `version`, marks the prior row
`is_current=False`, and never overwrites it.

## 13. Stage 2 — 13-Week Cash Flow Forecast Engine

Full detail lives in `FORECAST_ENGINE.md` (architecture),
`FORECAST_METHODOLOGY.md` (calculation methodology), and
`FORECAST_DATA_SOURCES.md` (exactly which Stage 0/1 data feeds it). This
section is a short pointer plus the two most important architectural
decisions.

**Consolidated weeks, projected views.** `ForecastWeek` stores one row per
week — the consolidated, reporting-currency waterfall for the forecast's
whole scope. Entity-level and currency-level views are never separately
stored; they're computed live from `ForecastLine` (which carries both the
original transaction currency and the FX-converted reporting amount on
every row). This is what guarantees a currency-specific liquidity
shortfall can never be hidden by a healthy consolidated figure — the
currency view sums un-converted native amounts, not the converted ones.

**Versioning via parent linkage, not mutation.** Rolling a forecast
forward creates a new `Forecast` row (`version += 1`,
`parent_forecast_id` pointing at the one it rolled from) rather than
mutating the existing one — the same "never silently overwrite historical
financial data" principle applied to `FXRate` in the foundation stage. A `PUBLISHED`
forecast additionally refuses recalculation outright.

**RBAC reuse.** No new permission enum values were needed — the foundation stage
already defined `TreasuryModule.FORECAST_13WK`, and the standard
`TreasuryAction` set (VIEW/CREATE/EDIT/APPROVE/ADJUST/EXPORT/CONFIGURE)
covers every permission the spec names (FORECAST_PUBLISH → APPROVE,
FORECAST_SCENARIO/FORECAST_ADMIN → CONFIGURE, etc.). Endpoints whose
target entity comes from the request body or a loaded record (rather than
a URL path parameter) authenticate via `get_current_user` and call
`_grants_permission(...)` explicitly, the same pattern established in
Stage 1 §12.2 for the same reason.

