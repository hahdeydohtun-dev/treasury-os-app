"""
Tests for the test-database safety guard (tests/_db_safety.py).

Pure tests exercise configuration validation. Live tests use real PostgreSQL scratch databases to prove the
connection-time check runs BEFORE any destructive SQL and that an unmarked database is never truncated.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests._db_safety import (
    DISPOSABLE_MARKER,
    ENV_CONFIRM,
    ENV_EXTRA_HOSTS,
    ENV_URL,
    UnsafeTestDatabaseError,
    configure_test_database,
    create_scratch_database,
    drop_scratch_database,
    evaluate_test_database_config,
    get_approved_test_database,
    guarded_truncate,
    mark_database_disposable,
    scratch_urls,
    verify_connected_database,
    with_database,
)

GOOD_URL = "postgresql+asyncpg://tester:pw@localhost:5432/treasury_os_test"


def _env(**overrides):
    env = {ENV_URL: GOOD_URL, ENV_CONFIRM: "treasury_os_test"}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


# --------------------------------------------------------------------------------------------------
# 1. an explicitly configured disposable test database is allowed
# --------------------------------------------------------------------------------------------------
def test_explicitly_configured_disposable_database_is_approved():
    approved = evaluate_test_database_config(_env(), forbidden_names={"treasury_os"})
    assert (approved.name, approved.host, approved.port, approved.username) == ("treasury_os_test", "localhost", 5432, "tester")
    assert approved.async_url.startswith("postgresql+asyncpg://") and approved.sync_url.startswith("postgresql+psycopg2://")
    assert approved.sync_url.endswith("/treasury_os_test")


@pytest.mark.parametrize("name", ["test_treasury", "treasury_os_test", "treasury_test_ci", "ci_test_run1"])
def test_naming_convention_variants_are_approved(name):
    url = f"postgresql+asyncpg://u:p@127.0.0.1:5432/{name}"
    assert evaluate_test_database_config({ENV_URL: url, ENV_CONFIRM: name}).name == name


def test_explicitly_allowed_non_loopback_host_is_approved_only_when_listed():
    url = "postgresql+asyncpg://u:p@postgres-ci:5432/treasury_os_test"
    with pytest.raises(UnsafeTestDatabaseError, match="not loopback"):
        evaluate_test_database_config({ENV_URL: url, ENV_CONFIRM: "treasury_os_test"})
    approved = evaluate_test_database_config(
        {ENV_URL: url, ENV_CONFIRM: "treasury_os_test", ENV_EXTRA_HOSTS: "postgres-ci, other"})
    assert approved.host == "postgres-ci"


def test_configure_repoints_the_application_settings_only_when_approved():
    settings_obj = SimpleNamespace(DATABASE_URL="postgresql+asyncpg://u:p@localhost/treasury_os",
                                   DATABASE_URL_SYNC="postgresql+psycopg2://u:p@localhost/treasury_os")
    approved, error = configure_test_database(settings_obj, _env())
    assert error is None and approved is not None
    assert settings_obj.DATABASE_URL == approved.async_url and settings_obj.DATABASE_URL.endswith("/treasury_os_test")
    assert settings_obj.DATABASE_URL_SYNC == approved.sync_url


def test_configuring_the_same_settings_twice_is_idempotent_and_still_compares_to_the_original_url():
    """Regression: a second configure call (e.g. a module imported twice) must not mistake the already re-pointed
    settings for the application's own database, and must still reject the application's real database."""
    settings_obj = SimpleNamespace(DATABASE_URL="postgresql+asyncpg://u:p@localhost/treasury_os",
                                   DATABASE_URL_SYNC="postgresql+psycopg2://u:p@localhost/treasury_os")
    first, err1 = configure_test_database(settings_obj, _env())
    second, err2 = configure_test_database(settings_obj, _env())
    assert err1 is None and err2 is None and first == second
    dev_url = "postgresql+asyncpg://u:p@localhost:5432/treasury_os"
    refused, err3 = configure_test_database(settings_obj, {ENV_URL: dev_url, ENV_CONFIRM: "treasury_os"})
    assert refused is None and "reserved" in str(err3)


def test_configuring_fake_settings_never_replaces_the_registered_real_approval():
    before = get_approved_test_database()
    configure_test_database(SimpleNamespace(DATABASE_URL="postgresql+asyncpg://u:p@localhost/treasury_os",
                                            DATABASE_URL_SYNC="s"), _env())
    assert get_approved_test_database() is before and before is not None and before.username != "tester"


# --------------------------------------------------------------------------------------------------
# 2. missing test-only configuration is rejected (fail closed)
# --------------------------------------------------------------------------------------------------
def test_missing_test_url_is_rejected_and_never_falls_back_to_the_application_url():
    with pytest.raises(UnsafeTestDatabaseError, match=ENV_URL):
        evaluate_test_database_config({"DATABASE_URL": GOOD_URL, ENV_CONFIRM: "treasury_os_test"})
    settings_obj = SimpleNamespace(DATABASE_URL="postgresql+asyncpg://u:p@localhost/treasury_os", DATABASE_URL_SYNC="x")
    approved, error = configure_test_database(settings_obj, {"DATABASE_URL": GOOD_URL})
    assert approved is None and isinstance(error, UnsafeTestDatabaseError)
    assert settings_obj.DATABASE_URL.endswith("/treasury_os")     # untouched on refusal


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_test_url_is_rejected(value):
    with pytest.raises(UnsafeTestDatabaseError):
        evaluate_test_database_config(_env(**{ENV_URL: value}))


@pytest.mark.parametrize("confirm", [None, "", "treasury_os", "TREASURY_OS_TEST", "treasury_os_test "[:-1] + "x"])
def test_missing_or_wrong_confirmation_is_rejected(confirm):
    with pytest.raises(UnsafeTestDatabaseError, match=ENV_CONFIRM):
        evaluate_test_database_config(_env(**{ENV_CONFIRM: confirm}))


def test_configure_never_mutates_settings_when_configuration_is_incomplete():
    settings_obj = SimpleNamespace(DATABASE_URL="postgresql+asyncpg://u:p@localhost/treasury_os", DATABASE_URL_SYNC="s")
    approved, error = configure_test_database(settings_obj, {ENV_URL: GOOD_URL})   # confirmation missing
    assert approved is None and error is not None and settings_obj.DATABASE_URL.endswith("/treasury_os")


# --------------------------------------------------------------------------------------------------
# 3. production-like or unapproved URLs are rejected
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url, reason", [
    ("postgresql+asyncpg://u:p@db.prod.example.com:5432/treasury_os_test", "not loopback"),
    ("postgresql+asyncpg://u:p@10.1.2.3:5432/treasury_os_test", "not loopback"),
    ("postgresql+asyncpg://u:p@localhost:5432/treasury_os", "reserved"),
    ("postgresql+asyncpg://u:p@localhost:5432/postgres", "reserved"),
    ("postgresql+asyncpg://u:p@localhost:5432/treasury_os_production_test", "production-like"),
    ("postgresql+asyncpg://u:p@localhost:5432/prod_test", "production-like"),
    ("postgresql+asyncpg://u:p@localhost:5432/live_test", "production-like"),
    ("postgresql+asyncpg://u:p@localhost:5432/treasury_data", "naming convention"),
    ("postgresql+asyncpg://u:p@localhost:5432/", "does not name a database"),
    ("postgresql+asyncpg://u:p@localhost:5432/Treasury_Test", "lowercase"),
    ('postgresql+asyncpg://u:p@localhost:5432/x"; DROP DATABASE y; --', "lowercase"),
    ("postgresql://u:p@localhost:5432/treasury_os_test", "asyncpg"),
    ("sqlite+aiosqlite:///test.db", "asyncpg"),
    ("postgresql+asyncpg://u:p@localhost:5432/treasury_os_test?host=db.prod.example.com", "query string"),
    ("postgresql+asyncpg://u:p@/treasury_os_test", "name a host"),
    ("not a url", "not a valid database URL"),
])
def test_unapproved_or_production_like_urls_are_rejected(url, reason):
    name = url.rsplit("/", 1)[-1].split("?")[0] or "x"
    with pytest.raises(UnsafeTestDatabaseError, match=reason):
        evaluate_test_database_config({ENV_URL: url, ENV_CONFIRM: name}, forbidden_names={"treasury_os"})


def test_the_applications_own_configured_database_is_rejected_even_if_it_looks_like_a_test_database():
    with pytest.raises(UnsafeTestDatabaseError, match="application itself is configured"):
        evaluate_test_database_config(_env(), forbidden_names={"treasury_os_test"})


def test_production_like_host_is_rejected_even_when_explicitly_allowed():
    url = "postgresql+asyncpg://u:p@db.prod.example.com:5432/treasury_os_test"
    with pytest.raises(UnsafeTestDatabaseError, match="production-like"):
        evaluate_test_database_config({ENV_URL: url, ENV_CONFIRM: "treasury_os_test",
                                       ENV_EXTRA_HOSTS: "db.prod.example.com"})


# --------------------------------------------------------------------------------------------------
# 4. the guard runs BEFORE any destructive SQL (recording fake session: proves the order of statements)
# --------------------------------------------------------------------------------------------------
class _Row:
    def __init__(self, values):
        self._values = values

    def one(self):
        return self._values


class RecordingSession:
    """Records every statement; answers the guard's verification query with a scripted row."""
    def __init__(self, verification_row):
        self.statements: list[str] = []
        self._row = verification_row

    async def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.statements.append(sql)
        return _Row(self._row) if "current_database()" in sql else _Row(())


def _approved(name="treasury_os_test"):
    return evaluate_test_database_config({ENV_URL: f"postgresql+asyncpg://u:p@localhost:5432/{name}", ENV_CONFIRM: name})


async def _truncate(session, approved, url="postgresql+asyncpg://u:p@localhost:5432/treasury_os_test"):
    from sqlalchemy.engine import make_url
    await guarded_truncate(session, approved, "TRUNCATE TABLE canary", make_url(url))


async def test_no_sql_at_all_is_sent_when_no_database_is_approved():
    session = RecordingSession(("treasury_os_test", DISPOSABLE_MARKER, None, "16"))
    with pytest.raises(UnsafeTestDatabaseError, match="No approved test database"):
        await _truncate(session, None)
    assert session.statements == []                                  # not even the verification SELECT


async def test_unmarked_database_is_rejected_before_truncate_is_sent():
    session = RecordingSession(("treasury_os_test", None, None, "16"))   # database comment is NULL
    with pytest.raises(UnsafeTestDatabaseError, match="not marked disposable"):
        await _truncate(session, _approved())
    assert len(session.statements) == 1 and "current_database()" in session.statements[0]
    assert not any("TRUNCATE" in s for s in session.statements)


async def test_wrong_marker_is_rejected_before_truncate_is_sent():
    session = RecordingSession(("treasury_os_test", "some other comment", None, "16"))
    with pytest.raises(UnsafeTestDatabaseError, match="not marked disposable"):
        await _truncate(session, _approved())
    assert not any("TRUNCATE" in s for s in session.statements)


async def test_connected_to_a_different_database_is_rejected_before_truncate_is_sent():
    session = RecordingSession(("treasury_os", DISPOSABLE_MARKER, None, "16"))   # marked, but not the approved name
    with pytest.raises(UnsafeTestDatabaseError, match="not the approved test database"):
        await _truncate(session, _approved())
    assert not any("TRUNCATE" in s for s in session.statements)


async def test_non_loopback_server_is_rejected_before_truncate_is_sent():
    session = RecordingSession(("treasury_os_test", DISPOSABLE_MARKER, "203.0.113.9/32", "16"))
    with pytest.raises(UnsafeTestDatabaseError, match="not loopback"):
        await _truncate(session, _approved())
    assert not any("TRUNCATE" in s for s in session.statements)


async def test_engine_pointing_at_another_database_is_rejected_before_any_sql():
    session = RecordingSession(("treasury_os_test", DISPOSABLE_MARKER, None, "16"))
    with pytest.raises(UnsafeTestDatabaseError, match="approved test database is"):
        await _truncate(session, _approved(), url="postgresql+asyncpg://u:p@localhost:5432/treasury_os")
    assert session.statements == []


async def test_approved_database_runs_verification_first_then_truncate():
    session = RecordingSession(("treasury_os_test", DISPOSABLE_MARKER, "127.0.0.1/32", "16"))
    await _truncate(session, _approved())
    assert len(session.statements) == 2
    assert "current_database()" in session.statements[0] and "TRUNCATE" in session.statements[1]   # order


# --------------------------------------------------------------------------------------------------
# Live PostgreSQL: the connection-time marker check with real databases
# --------------------------------------------------------------------------------------------------
@pytest.fixture
def approved_server():
    approved = get_approved_test_database()
    assert approved is not None                          # pytest_configure already refuses to run otherwise
    return approved


async def _canary_database(approved_server, *, marked):
    name = create_scratch_database(approved_server, marked=marked)
    async_url, _ = scratch_urls(approved_server, name)
    engine = create_async_engine(async_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE canary (id int)"))
        await conn.execute(text("INSERT INTO canary VALUES (1), (2), (3)"))
    return name, engine


async def test_real_unmarked_database_is_not_truncated_but_the_same_database_once_marked_is(approved_server):
    name, engine = await _canary_database(approved_server, marked=False)
    try:
        scratch_approval = with_database(approved_server, name)
        async with AsyncSession(engine) as session:
            with pytest.raises(UnsafeTestDatabaseError, match="not marked disposable"):
                await guarded_truncate(session, scratch_approval, "TRUNCATE TABLE canary", engine.url)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM canary"))).scalar_one() == 3   # NOT truncated

        mark_database_disposable(approved_server, name)       # control: the guard is not vacuous
        async with AsyncSession(engine) as session:
            await guarded_truncate(session, scratch_approval, "TRUNCATE TABLE canary", engine.url)
            await session.commit()
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM canary"))).scalar_one() == 0
    finally:
        await engine.dispose()
        drop_scratch_database(approved_server, name)


async def test_live_verification_reports_the_actual_connection(approved_server):
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        info = await verify_connected_database(session, approved_server)
    assert info["database"] == approved_server.name and info["marker"] == DISPOSABLE_MARKER
    assert info["server_version"].split(".")[0].isdigit()


async def test_scratch_helpers_refuse_anything_that_is_not_a_scratch_database(approved_server):
    for bad in (approved_server.name, "treasury_os", "postgres", "template1", "treasury_os_test_x"):
        with pytest.raises(UnsafeTestDatabaseError, match="not a scratch database"):
            drop_scratch_database(approved_server, bad)
    # a scratch-prefixed database that this process did not create and that is not marked is also refused
    stranger = create_scratch_database(approved_server, marked=False)
    try:
        from tests import _db_safety
        _db_safety._CREATED_THIS_PROCESS.discard(stranger)
        with pytest.raises(UnsafeTestDatabaseError, match="not created here and not marked"):
            drop_scratch_database(approved_server, stranger)
    finally:
        _db_safety._CREATED_THIS_PROCESS.add(stranger)
        drop_scratch_database(approved_server, stranger)


# --------------------------------------------------------------------------------------------------
# Trace guard: no destructive operation may appear in the suite outside the guarded paths
# --------------------------------------------------------------------------------------------------
_DESTRUCTIVE = re.compile(
    r"TRUNCATE|DROP\s+(TABLE|DATABASE|SCHEMA|INDEX)|DELETE\s+FROM|create_all|drop_all|create_async_engine|"
    r"create_engine\(|psycopg2\.connect|asyncpg\.connect", re.IGNORECASE)
# conftest.py holds the one guarded TRUNCATE; _db_safety.py IS the guard; this file mentions the patterns on
# purpose. test_migration_accepted_claims_preflight.py is allowed because it creates/drops databases only through
# the guard's scratch helpers and runs its own SQL (including one DELETE) exclusively against scratch databases
# through ScratchDb, which refuses any database name without the scratch prefix.
_ALLOWED = {"conftest.py", "_db_safety.py", "test_database_safety_guard.py", "test_migration_accepted_claims_preflight.py"}


def test_no_unguarded_destructive_operation_exists_in_the_test_suite():
    offenders = []
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.name in _ALLOWED:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if _DESTRUCTIVE.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert offenders == [], "destructive operation outside the guarded paths:\n" + "\n".join(offenders)


def test_the_only_truncate_in_conftest_goes_through_guarded_truncate():
    source = (Path(__file__).parent / "conftest.py").read_text()
    assert source.count("TRUNCATE TABLE") == 1 and source.count("guarded_truncate(") == 1
    assert source.index("guarded_truncate(") < source.index("TRUNCATE TABLE")
    assert "session.execute(\n            text(\n                \"TRUNCATE" not in source        # no direct execute remains
