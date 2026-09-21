# Treasury OS — Foundation

Multi-entity, multi-currency Treasury Management System. This is the
**foundation stage only**: project structure, auth, RBAC, currency/FX admin,
audit trail, and the frontend shell. Treasury modules (payments,
reconciliation, forecasting, etc.) are not yet implemented — see
`DEVELOPMENT_ROADMAP.md`.

## Stack

- Backend: Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, PostgreSQL
- Frontend: Next.js 14 (App Router), React, TypeScript
- Background processing (wired for later use): Redis, Celery
- Auth: JWT access/refresh tokens, entity-scoped RBAC

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
cp .env.example .env 2>/dev/null || true   # or copy from repo-root .env.example
```

Ensure `backend/.env` has (defaults already match the Docker Compose values):

```
DATABASE_URL=postgresql+asyncpg://treasury:treasury@localhost:5432/treasury_os
DATABASE_URL_SYNC=postgresql+psycopg2://treasury:treasury@localhost:5432/treasury_os
SECRET_KEY=<replace with a real secret>
```

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

(11 tests: health check, password/JWT security, login flow, entity-scoped
RBAC authorization rules, and FX-rate versioning — confirms a corrected FX
rate creates a new version and never overwrites the original row.)

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

The foundation ships with no seeded users. Create one directly against the
API (there is no public signup endpoint by design — user provisioning is an
administrative action):

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

A superuser bypasses RBAC scoping entirely (see
`app/auth/dependencies.py::_grants_permission`). For a non-superuser, you
also need to create a `Role`, attach `Permission` rows for the
(module, action) pairs they need, and a `UserRoleAssignment` binding the
user to that role at `GROUP_WIDE` or `ENTITY` scope. This will get a proper
admin UI/endpoint in a later stage — for now it's direct DB/ORM work.

Then log in at http://localhost:3000/login with `admin@example.com` /
`ChangeMe123!`.

## What's verified working

- `alembic upgrade head` runs clean against a fresh Postgres 16 database
  (32 tables as of Stage 2, plus Stage 2 Hardening).
- `pytest -q` → 52 passed.
- `uvicorn app.main:app` starts and serves `/health`, `/api/v1/currencies`,
  and the full Stage 0-2 API surface.
- `npm run build` compiles the frontend with no errors (10 routes).
- `npm run start` serves `/dashboard`, `/login`, `/cash-liquidity`,
  `/banks-accounts`, `/excel-data-hub`, and `/forecast` (200 OK), talking
  to the live backend.
- The Excel upload → validate → preview → confirm → import workflow was
  exercised end-to-end against a real `.xlsx` file (Bank Balances
  template), including a deliberately invalid currency, an unknown
  account, and a duplicate row — all correctly flagged and excluded from
  import, with only the valid row actually landing in the database.
- FX rate import via Excel confirmed to create a new `FXRate` version
  rather than overwrite the prior one, same as the original FXRate API path.
- The forecast engine's full lifecycle was exercised live: create →
  calculate → publish → recalculation-on-published correctly rejected →
  roll-forward creates a linked next version → what-if creates a separate
  scratch forecast and returns a week-by-week comparison without touching
  the base. The demo dataset's currency view was confirmed to show a
  genuine NGN liquidity gap (weeks 3-7) underneath a healthy
  USD-consolidated figure — proving the "don't hide a currency shortfall"
  requirement actually holds, not just in theory.

## Seeding data

Four seed scripts, run from `backend/` with the venv active, in order:

```bash
python -m app.db.seed_reference_data     # account types, cash event types,
                                           # common currencies, Excel template
                                           # registry rows - required for the
                                           # app to be useful at all
python -m app.db.seed_forecast_categories # default cash-flow category hierarchy
python -m app.db.seed_demo_data           # DEMO DATA: 2 entities, 2 banks,
                                           # 3 accounts, transactions, expected
                                           # flows, and 2 demo users - optional,
                                           # for trying the app out
python -m app.db.seed_forecast_demo_data  # DEMO DATA: liquidity thresholds,
                                           # recurring payroll/opex, a 13-week
                                           # spread with a deliberate deficit
                                           # and recovery, and a calculated
                                           # demo forecast - optional
```

Demo users created by `seed_demo_data`:

| Email | Password | Scope |
|---|---|---|
| `demo-admin@treasuryos.example.com` | `DemoPassword123!` | Group-wide |
| `demo-entitya@treasuryos.example.com` | `DemoPassword123!` | Entity A only |

Everything the demo seed scripts create is prefixed `[DEMO]` (names) or
tagged `source_type="DEMO_DATA"` — never presented as real financial data.

## What's NOT implemented yet

Payments, bank reconciliation, intercompany reconciliation, investments,
loans/facilities, working capital, KPIs, reports, tasks/workflow, and the
AI Treasury Copilot are all out of scope through Stage 2, per the build
instructions. See `DEVELOPMENT_ROADMAP.md` for the planned build order —
Funding & Credit Facilities is next.

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
