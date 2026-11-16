# Test database safety

The backend test suite is **destructive**. An autouse fixture runs `TRUNCATE ... CASCADE` over `users`,
`currencies`, `groups`, `legal_entities`, `bank_accounts` and more **before every test**, and the migration tests
create and drop databases. The tests therefore must only ever run against a database created for that purpose.

Until this guard existed the suite simply used `settings.DATABASE_URL` - the application's own database, which
defaults to the development database `treasury_os`. Running the tests with the default configuration would have
wiped it.

## What is enforced (fail closed)

A destructive operation proceeds only when **all** of these hold; any missing or ambiguous input aborts.

| # | Check | Where |
|---|---|---|
| 1 | `TREASURY_TEST_DATABASE_URL` is set. The application's `DATABASE_URL` is **never** used by the tests. | startup |
| 2 | `TREASURY_TEST_CONFIRM_DESTRUCTIVE` equals the database name exactly (typed confirmation). | startup |
| 3 | The URL is structurally acceptable: `postgresql+asyncpg`; **no query string** (it could override the host); host is loopback (`localhost`, `127.0.0.1`, `::1`) or listed in `TREASURY_TEST_EXTRA_ALLOWED_HOSTS`; name is lowercase `[a-z][a-z0-9_]*`, follows the convention (`test_*`, `*_test`, or contains `_test_`), is not reserved (`treasury_os`, `postgres`, `template0/1`), contains no production-like fragment (`prod`, `live`, `staging`, `preprod`, `uat`) in the name or host, and is not the database the application itself is configured to use. | startup |
| 4 | **Live check over the real connection, before any destructive SQL:** `current_database()` matches; the server address is loopback (or an explicitly allowed host); and the database carries the marker `treasury-os:disposable-test-database:v1` in its own comment. | before every `TRUNCATE` |

Check 4 is the strongest: it is stored *in the database*, so a mis-pointed URL is refused even if every name and
host check passes, because a real database does not have that comment. No single check, including the name, is
trusted on its own.

If checks 1-3 fail, `pytest` exits with code **4** and a message before any test starts (collection-only runs
such as `pytest --collect-only` never touch the database and are still allowed). If check 4 fails, the fixture
raises `UnsafeTestDatabaseError` before the `TRUNCATE` is sent, and the test errors.

## One-time setup of a disposable database

```bash
# 1. create it and MARK it disposable (the marker is mandatory)
psql -U postgres -c "CREATE DATABASE treasury_os_test OWNER treasury;"
psql -U postgres -c "COMMENT ON DATABASE treasury_os_test IS 'treasury-os:disposable-test-database:v1';"

# 2. migrate it (Alembic reads DATABASE_URL_SYNC)
cd backend
export DATABASE_URL=postgresql+asyncpg://treasury:treasury@localhost:5432/treasury_os_test
export DATABASE_URL_SYNC=postgresql+psycopg2://treasury:treasury@localhost:5432/treasury_os_test
alembic upgrade head

# 3. load the REFERENCE data the tests rely on (these lookup tables are not truncated by the fixture)
python -m app.db.seed_reference_data
python -m app.db.seed_forecast_categories
python -m app.db.seed_facility_types
python -m app.db.seed_investment_types
unset DATABASE_URL DATABASE_URL_SYNC     # the tests never use these; do not leave them pointing at the test database
```

Do **not** run `seed_demo_data` against the test database: demo rows are truncated before every test anyway. Without
step 3 about four tests fail with foreign-key errors (`forecast_categories`, `cash_event_types`): a migrated but
unseeded database is not enough.

The database role needs `CREATEDB` (the migration tests create `treasury_test_tmp_*` scratch databases on the
same server and drop them afterwards).

## Running the tests

```bash
cd backend
export TREASURY_TEST_DATABASE_URL=postgresql+asyncpg://treasury:treasury@localhost:5432/treasury_os_test
export TREASURY_TEST_CONFIRM_DESTRUCTIVE=treasury_os_test
python -m pytest -q
```

Use `python -m pytest`: the bare `pytest` command that the README previously showed fails with
`ModuleNotFoundError: No module named 'app'` because the backend directory is not on `sys.path`.

Do not run two pytest sessions against the same server at once: the migration tests sweep leftover *marked*
`treasury_test_tmp_*` scratch databases at start and would remove the other session's.

## Migration tests

`tests/test_migration_accepted_claims_preflight.py` never touches the shared test database. It migrates a
template database to the previous head, clones it per test, writes historical decisions with SQL, and runs
`alembic upgrade head` in a subprocess whose environment points at the clone (the subprocess first reports which
database its settings resolve to and the test aborts unless it is the intended scratch database). Scratch
databases are named `treasury_test_tmp_<random>`; only databases with that prefix that were created by the process
or carry the marker can be dropped.

## What this guard covers - and what it does not

Traced and verified for this repository:

* The suite contains **one** destructive statement: the `TRUNCATE` in `tests/conftest.py`, which now goes through
  `guarded_truncate`. A test (`test_no_unguarded_destructive_operation_exists_in_the_test_suite`) fails if another
  `TRUNCATE`, `DROP`, `DELETE FROM`, `create_all`/`drop_all`, or a hand-built engine/connection appears in any
  test module outside the allowlist.
* Every test reaches the database through `app.db.session` (the module-level engine), which `conftest.py`
  re-points at the approved URL before it is built, and the guard verifies engine and live connection before the
  `TRUNCATE`.

Not covered: a hand-written script, a seed command, `alembic` run manually, or code outside `tests/` that someone
points at a database. The guard protects the test suite, nothing else. It also cannot prove a database is
*empty or unimportant* - only that it was explicitly marked and confirmed as disposable by whoever set it up.
Marking a real database with the marker comment defeats check 4; do not do that.
