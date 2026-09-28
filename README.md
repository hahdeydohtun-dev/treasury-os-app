# Treasury OS

Multi-entity, multi-currency Treasury Management System.

## Stage status

| Stage | Status |
|---|---|
| Stage 0 — Foundation | Complete |
| Stage 1 — Treasury Data Foundation + Excel Data Hub | Complete |
| Stage 2 — 13-Week Cash Flow Forecast Engine | Complete |
| Stage 2 Hardening — RBAC / entity isolation / security | Complete |
| Stage 3 — Funding & Credit Facilities | Complete |
| Stage 3 Hardening — integrity / concurrency / idempotency / repository security | Complete |
| Stage 4 — Investments & Fixed Deposit Management | Complete (including financial-integrity/cash-ledger hardening) |
| Stage 5A — Bank Statement Ingestion & Normalization | Complete |
| Stage 5B — Reconciliation Data Model | Complete |
| Stage 5C onward — Deterministic Matching, Advanced Matching, Open Items, Reports, Adaptive Learning | **Not Started** |

See `DEVELOPMENT_ROADMAP.md` for the full build history and what's planned
next, and `docs/` for a per-stage architecture doc and implementation
report (e.g. `docs/STAGE_4_INVESTMENTS.md`,
`docs/STAGE_4_IMPLEMENTATION_REPORT.md`).

## Stack

- Backend: Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, PostgreSQL
- Frontend: Next.js 14 (App Router), React, TypeScript
- Background processing (wired for later use): Redis, Celery
- Auth: JWT access/refresh tokens, entity-scoped RBAC

## Architecture at a glance

- **Cash ledger**: every module (bank transactions, facility drawdowns/
  repayments, investment placements/terminations/maturities) feeds the
  *same single* `TreasuryTransaction` table
  (`app/models/treasury_transaction.py`) via `event_type_code` and
  `source_type`/`source_record_id` - there is no per-module parallel cash
  ledger anywhere in the codebase.
- **Cash position**: `app/services/cash_position_service.py` derives
  available/total cash from the latest reported `BankBalance` per
  account - the authoritative source every cash-sufficiency check (e.g.
  investment placement) uses server-side, never a client-supplied figure.
  `calculate_operational_available_cash` additionally rolls forward any
  posted `TreasuryTransaction` movements dated after that balance's own
  `balance_date` (documented convention: a balance dated `D` is treated
  as already reflecting everything through `D` - see
  `app/models/balance.py::BankBalance` and
  `docs/STAGE_4_INVESTMENTS.md`), without ever mutating `BankBalance`
  itself or creating a second cash ledger.
- **Forecast engine**: `app/services/forecast_engine.py` is the single
  13-week forecast calculator; each module (facilities, investments)
  feeds it through its own small, clean adapter
  (`facility_forecast_adapter.py`, `investment_forecast_adapter.py`)
  rather than the engine having module-specific logic baked in.
- **RBAC/entity isolation**: `app/auth/authorization.py` is the single
  authorization module every endpoint in every stage uses -
  `assert_entity_access` for single-record operations,
  `get_authorized_scope`/`resolve_scope_entity_ids`/
  `apply_resolved_entity_scope` for lists and aggregates. No module has
  ever introduced a second authorization system.
- **Versioning**: material commercial terms (FX rates, facility terms,
  investment terms) are never overwritten in place - a change always
  creates a new version row, and the current record's own fields are the
  only thing endpoints read for "current" values.
- **Financial calculations**: `Decimal` throughout, never floating point.

## Investment lifecycle (Stage 4)

`Investment.status`: `DRAFT -> SUBMITTED -> UNDER_REVIEW -> APPROVED ->
PLACEMENT_PENDING -> ACTIVE -> {MATURED, PARTIALLY_TERMINATED,
TERMINATED, ROLLED_OVER, REBOOKED}`, enforced by an explicit transition
table. Creating an investment never places it; placement, termination,
rollover, rebooking, and maturity settlement are all separate, explicit,
row-locked, idempotent actions - see `docs/STAGE_4_INVESTMENTS.md` for
the full design.

**Cash integration**: placement creates a real `OUTFLOW`
`TreasuryTransaction` from the source bank account (validated against
that account's real available cash and currency - never trusted from the
client); termination creates an `INFLOW` reconciling exactly to net
proceeds; maturity settlement is an explicit action (never automatic on
the date passing) creating an `INFLOW` for principal + interest;
rollover creates exactly one `NON_CASH` traceability record (never two
offsetting real cash movements, since the money never actually leaves
the entity); rebooking's additional principal is a real `OUTFLOW`. Every
`InvestmentTransaction` that has a real cash effect links to its
`TreasuryTransaction` via `cash_transaction_id`.

## Multi-entity / multi-currency

Every facility and investment belongs to exactly one legal entity and
carries its own transaction currency - never silently converted. RBAC is
enforced at every layer (single-record, list, aggregate, Excel import,
forecast) for every module, verified by dedicated cross-entity and
cross-group regression tests throughout the test suite.

## Prerequisites

- Python 3.12+
- Node.js 20+
- PostgreSQL 16 (locally installed, or via Docker — see below)
- Redis 7 (only required once background jobs are added; not used yet)

## 1. Start Postgres (and Redis)

Using Docker:

```bash
docker compose up -d
```

This starts Postgres on `localhost:5432` (db `treasury_os`, user/pass
`treasury`/`treasury`) and Redis on `localhost:6379`.

If you don't have Docker, install Postgres locally and create the DB/user
yourself:

```sql
CREATE USER treasury WITH PASSWORD 'treasury' SUPERUSER;
CREATE DATABASE treasury_os OWNER treasury;
```

## 2. Backend setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp ../.env.example .env          # then edit SECRET_KEY etc. for your environment
```

`backend/.env` needs at minimum:

```
DATABASE_URL=postgresql+asyncpg://treasury:treasury@localhost:5432/treasury_os
DATABASE_URL_SYNC=postgresql+psycopg2://treasury:treasury@localhost:5432/treasury_os
SECRET_KEY=<replace with a real secret>
```

`backend/.env` is git-ignored - never commit it. Only `.env.example`
(root) is tracked, containing safe placeholder values.

Run migrations:

```bash
alembic upgrade head
```

Run the API:

```bash
uvicorn app.main:app --reload --port 8000
```

- API docs: http://localhost:8000/api/v1/docs
- Health check: http://localhost:8000/health

Run tests:

```bash
pytest -q
```

209 tests across Stage 0-5B (facilities, funding actions, forecast
engine, security/RBAC hardening, Excel Data Hub, investments, Stage
3/4 financial-integrity/concurrency hardening passes, bank statement
ingestion, and the reconciliation data model) - all passing as of this
stage.

## 3. Frontend setup

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

Open http://localhost:3000 — it redirects to `/dashboard`.

`.env.local` should contain:

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
```

## 4. Create your first user

There is no public signup endpoint by design — user provisioning is an
administrative action. Create one directly against the API:

```bash
cd backend
source .venv/bin/activate
python - <<'EOF'
import asyncio
from app.db.session import AsyncSessionLocal
from app.core.security import hash_password
from app.models.rbac import User

async def main():
    async with AsyncSessionLocal() as db:
        user = User(
            email="admin@example.com",
            full_name="Admin User",
            hashed_password=hash_password("ChangeMe123!"),
            is_superuser=True,
        )
        db.add(user)
        await db.commit()
        print("Created:", user.email)

asyncio.run(main())
EOF
```

A superuser bypasses RBAC scoping entirely. For a non-superuser, you
also need to create a `Role`, attach `Permission` rows for the
(module, action) pairs they need, and a `UserRoleAssignment` binding the
user to that role at `GROUP_WIDE` or `ENTITY` scope. This is direct
DB/ORM work — there is no admin UI for it yet.

Then log in at http://localhost:3000/login with `admin@example.com` /
`ChangeMe123!`.

## Seeding data

Run from `backend/` with the venv active, in order:

```bash
python -m app.db.seed_reference_data     # account types, cash event types
                                           # (including investment placement/
                                           # termination/maturity/rollover
                                           # codes), currencies, Excel
                                           # template registry rows
python -m app.db.seed_forecast_categories # default cash-flow category hierarchy
python -m app.db.seed_facility_types      # configurable facility type lookup
python -m app.db.seed_investment_types    # configurable investment type
                                           # lookup (Fixed Deposit implemented;
                                           # other types seeded as inert rows)
python -m app.db.seed_demo_data           # DEMO DATA: entities, banks,
                                           # accounts, transactions, and demo
                                           # users - optional
python -m app.db.seed_forecast_demo_data  # DEMO DATA: liquidity thresholds,
                                           # recurring flows, a demo forecast
                                           # - optional
```

Demo users created by `seed_demo_data`:

| Email | Password | Scope |
|---|---|---|
| `demo-admin@treasuryos.example.com` | `DemoPassword123!` | Group-wide |
| `demo-entitya@treasuryos.example.com` | `DemoPassword123!` | Entity A only |

Everything the demo seed scripts create is prefixed `[DEMO]` (names) or
tagged `source_type="DEMO_DATA"` — never presented as real financial data.

## What's NOT implemented yet

Bank reconciliation MATCHING (candidate generation, scoring, open items,
reports, adaptive learning), intercompany reconciliation, working
capital, KPIs/reports beyond what's listed in each stage's own doc,
tasks/workflow, and the AI Treasury Copilot are out of scope — see
`DEVELOPMENT_ROADMAP.md` for the planned build order. Stage 5A (Bank
Statement Ingestion & Normalization) and Stage 5B (Reconciliation Data
Model — `ReconciliationRun`/`ReconciliationConfiguration`, run creation
and a row-locked execution boundary that only counts in-scope evidence)
are both complete — see `docs/STAGE_5A_BANK_STATEMENT_INGESTION.md` and
`docs/STAGE_5B_RECONCILIATION_DATA_MODEL.md` — but nothing yet reads that
evidence to actually reconcile it against `TreasuryTransaction`; that is
Stage 5B onward, not started. Each completed stage's own
`docs/STAGE_N_*.md`
lists that stage's specific known limitations in detail (e.g. investment
periodic-interest forecast SCHEDULES are implemented for MONTHLY/
QUARTERLY/SEMI_ANNUAL/ANNUAL frequencies — real payment dates, not a
lump sum at maturity — but actual periodic interest TRANSACTIONS are not
automatically posted on those dates; a treasury user or Excel import
still records each actual receipt through the existing transaction/
import workflow; CUSTOM frequency remains unsupported because no
explicit custom payment dates are stored; and actual bank-to-investment
receipt reconciliation remains a future capability, not built here — no
automated covenant-vs-forecast projection for facilities yet either).

## Remaining setup you'll need to do yourself

- Set a real `SECRET_KEY` in `backend/.env` before any non-local deployment.
- Decide on and implement a real user-provisioning workflow (invite flow,
  SSO, etc.) — the direct-DB approach above is a placeholder for local dev.
- Point `ANTHROPIC_API_KEY` / enable `AI_COPILOT_ENABLED` when the AI
  Treasury Copilot module is built.
- Redis/Celery are configured but nothing uses them yet; no separate setup
  is required until a background-job module is added.
- The dev setup here uses one Postgres database for both the app and the
  test suite (tests truncate their tables before each test run). Use a
  separate `treasury_os_test` database if you want the two fully
  isolated.
