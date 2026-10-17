"""
Test-database safety guard.

The backend test suite is destructive: an autouse fixture runs `TRUNCATE ... CASCADE` over users, currencies,
groups, legal entities, bank accounts and more before EVERY test, and the migration tests create and drop
databases. Historically the suite simply used `settings.DATABASE_URL` - whatever database the application was
configured for, which by default is the development database. This module makes that impossible.

It FAILS CLOSED. A destructive operation proceeds only when ALL of these hold:

  1. The explicit, test-only variable TREASURY_TEST_DATABASE_URL is set. The application's DATABASE_URL is never
     trusted for tests; if the test variable is missing the suite refuses to run.
  2. TREASURY_TEST_CONFIRM_DESTRUCTIVE equals the database name exactly (a typed confirmation tied to this DB).
  3. The URL is structurally acceptable: postgresql+asyncpg, no query string (which could override host/dbname),
     a loopback host (or one explicitly listed in TREASURY_TEST_EXTRA_ALLOWED_HOSTS), a safe lowercase name that
     follows the test naming convention, no production-like fragment in host or name, not a reserved name, and
     not the database the application itself is configured to use.
  4. LIVE CHECK, run over the real connection immediately before any destructive SQL: `current_database()`
     matches, the server address is loopback/unix-socket (or an explicitly allowed host), and the database
     carries the disposable-database MARKER in its own comment (`COMMENT ON DATABASE ... IS '<marker>'`).
     A real database does not have that comment, so a mis-pointed URL is refused even if every name check passes.

Checks 1-3 are pure functions of configuration; check 4 needs a connection and only ever issues SELECTs before it
either raises or lets the caller proceed. Scope: this guards every statement issued through the application's
engine/session in tests (all tests use it) and the scratch databases created by the migration tests. It does not
and cannot protect a database someone points a hand-written script at.
"""
from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

ENV_URL = "TREASURY_TEST_DATABASE_URL"
ENV_CONFIRM = "TREASURY_TEST_CONFIRM_DESTRUCTIVE"
ENV_EXTRA_HOSTS = "TREASURY_TEST_EXTRA_ALLOWED_HOSTS"

#: Stored as the database's own comment; the single strongest "this is disposable" signal.
DISPOSABLE_MARKER = "treasury-os:disposable-test-database:v1"
#: Every scratch database created by tests starts with this prefix; only such databases are ever dropped.
SCRATCH_DB_PREFIX = "treasury_test_tmp_"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_RESERVED_NAMES = frozenset({"treasury_os", "postgres", "template0", "template1"})
_PRODUCTION_FRAGMENTS = ("prod", "live", "staging", "preprod", "uat")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TEST_NAMING = re.compile(r"(^test_|_test$|_test_)")


class UnsafeTestDatabaseError(RuntimeError):
    """Raised, before any destructive SQL, whenever the test database is not provably disposable."""


@dataclass(frozen=True)
class ApprovedTestDatabase:
    async_url: str
    sync_url: str
    name: str
    host: str
    port: int | None
    username: str | None
    extra_hosts: frozenset[str] = frozenset()


def _database_name(raw: str | None) -> str | None:
    """The database name inside a URL, or None if absent/unparsable (an unparsable configured URL cannot be the test URL)."""
    if not raw:
        return None
    try:
        return make_url(raw).database or None
    except ArgumentError:
        return None


def _database_names(*urls: str | None) -> set[str]:
    return {name for name in map(_database_name, urls) if name}


def _contains_production_fragment(value: str) -> str | None:
    lowered = value.lower()
    return next((f for f in _PRODUCTION_FRAGMENTS if f in lowered), None)


def evaluate_test_database_config(
    env: Mapping[str, str], forbidden_names: set[str] | frozenset[str] = frozenset(),
) -> ApprovedTestDatabase:
    """Checks 1-3. Pure: no connection is made. Raises UnsafeTestDatabaseError with a specific reason."""
    raw = (env.get(ENV_URL) or "").strip()
    if not raw:
        raise UnsafeTestDatabaseError(
            f"{ENV_URL} is not set. The tests are destructive and never fall back to the application's "
            f"DATABASE_URL; point {ENV_URL} at a disposable database (see docs/TEST_DATABASE_SAFETY.md)."
        )
    try:
        url: URL = make_url(raw)
    except ArgumentError as exc:
        raise UnsafeTestDatabaseError(f"{ENV_URL} is not a valid database URL: {exc}") from None

    if url.drivername != "postgresql+asyncpg":
        raise UnsafeTestDatabaseError(f"{ENV_URL} must use the postgresql+asyncpg driver, not {url.drivername!r}.")
    if url.query:
        raise UnsafeTestDatabaseError(
            f"{ENV_URL} must not contain a query string ({sorted(url.query)}): options such as host= could "
            f"silently redirect the connection."
        )
    name = url.database or ""
    if not name:
        raise UnsafeTestDatabaseError(f"{ENV_URL} does not name a database.")
    if not _SAFE_NAME.match(name):
        raise UnsafeTestDatabaseError(
            f"Database name {name!r} must be lowercase letters, digits and underscores, starting with a letter."
        )
    if name in _RESERVED_NAMES:
        raise UnsafeTestDatabaseError(f"Database name {name!r} is reserved and can never be a test database.")
    fragment = _contains_production_fragment(name)
    if fragment:
        raise UnsafeTestDatabaseError(f"Database name {name!r} looks production-like (contains {fragment!r}).")
    if not _TEST_NAMING.search(name):
        raise UnsafeTestDatabaseError(
            f"Database name {name!r} does not follow the test naming convention "
            f"(starts with 'test_', ends with '_test', or contains '_test_')."
        )
    if name in forbidden_names:
        raise UnsafeTestDatabaseError(
            f"Database {name!r} is the database the application itself is configured to use (DATABASE_URL); "
            f"tests must never run against it."
        )

    extra_hosts = frozenset(h.strip().lower() for h in (env.get(ENV_EXTRA_HOSTS) or "").split(",") if h.strip())
    host = (url.host or "").lower()
    if not host:
        raise UnsafeTestDatabaseError(f"{ENV_URL} must name a host explicitly (loopback or an allowed host).")
    if host not in _LOOPBACK_HOSTS and host not in extra_hosts:
        raise UnsafeTestDatabaseError(
            f"Host {host!r} is not loopback and is not listed in {ENV_EXTRA_HOSTS}; remote databases are refused."
        )
    host_fragment = _contains_production_fragment(host)
    if host_fragment:
        raise UnsafeTestDatabaseError(f"Host {host!r} looks production-like (contains {host_fragment!r}).")

    confirmation = (env.get(ENV_CONFIRM) or "").strip()
    if confirmation != name:
        raise UnsafeTestDatabaseError(
            f"{ENV_CONFIRM} must equal the database name {name!r} exactly "
            f"(got {confirmation!r}). This typed confirmation is required for destructive tests."
        )

    return ApprovedTestDatabase(
        async_url=url.render_as_string(hide_password=False),
        sync_url=url.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False),
        name=name, host=host, port=url.port, username=url.username, extra_hosts=extra_hosts,
    )


# The application's configured URLs, captured the FIRST time a settings object is configured. Later calls (for
# example a module that is imported twice) must compare against these ORIGINAL values - the live settings object
# has by then been re-pointed at the test database and would otherwise be mistaken for "the application's own".
_ORIGINAL_URLS: dict[int, tuple[str | None, str | None]] = {}
_REGISTERED_APPROVAL: ApprovedTestDatabase | None = None


def register_approved_test_database(approved: ApprovedTestDatabase | None) -> None:
    """Called ONLY by tests/conftest.py for the real application settings. configure_test_database() deliberately does
    not register anything itself, so unit tests that configure fake settings objects can never replace the approval
    the live tests rely on."""
    global _REGISTERED_APPROVAL
    _REGISTERED_APPROVAL = approved


def get_approved_test_database() -> ApprovedTestDatabase | None:
    """The approval registered by conftest for the real run (None if none)."""
    return _REGISTERED_APPROVAL


def configure_test_database(
    settings_obj: Any, env: Mapping[str, str] | None = None,
) -> tuple[ApprovedTestDatabase | None, UnsafeTestDatabaseError | None]:
    """
    Re-point the application's settings at the approved test database BEFORE app.db.session builds its engine.
    On any problem the settings are left untouched and the error is returned for the caller to act on; nothing
    destructive is attempted here either way. Idempotent: configuring the same settings object again is safe.
    """
    env = os.environ if env is None else env
    original = _ORIGINAL_URLS.setdefault(
        id(settings_obj), (getattr(settings_obj, "DATABASE_URL", None), getattr(settings_obj, "DATABASE_URL_SYNC", None))
    )
    forbidden = _database_names(*original, env.get("DATABASE_URL"), env.get("DATABASE_URL_SYNC"))
    try:
        approved = evaluate_test_database_config(env, forbidden)
    except UnsafeTestDatabaseError as exc:
        return None, exc
    settings_obj.DATABASE_URL = approved.async_url
    settings_obj.DATABASE_URL_SYNC = approved.sync_url
    return approved, None


def check_engine_matches(engine_url: URL, approved: ApprovedTestDatabase) -> None:
    """The engine the fixtures are about to use must be the approved database (guards against import-order slips)."""
    if (engine_url.database != approved.name or (engine_url.host or "").lower() != approved.host
            or engine_url.port != approved.port or engine_url.username != approved.username):
        raise UnsafeTestDatabaseError(
            f"The application engine targets {engine_url.host}:{engine_url.port}/{engine_url.database} but the "
            f"approved test database is {approved.host}:{approved.port}/{approved.name}; refusing."
        )


async def verify_connected_database(session: Any, approved: ApprovedTestDatabase) -> dict[str, str | None]:
    """
    Check 4. Issues ONE read-only query over the live connection and raises unless the database is the approved,
    marked, disposable one. Only SELECTs happen before this returns or raises.
    """
    row = (await session.execute(text(
        "SELECT current_database(), "
        "(SELECT shobj_description(d.oid, 'pg_database') FROM pg_database d WHERE d.datname = current_database()), "
        "inet_server_addr()::text, current_setting('server_version')"
    ))).one()
    database, marker, server_addr, version = row[0], row[1], row[2], row[3]
    if database != approved.name:
        raise UnsafeTestDatabaseError(
            f"Connected to database {database!r}, not the approved test database {approved.name!r}; refusing."
        )
    if marker != DISPOSABLE_MARKER:
        raise UnsafeTestDatabaseError(
            f"Database {database!r} is not marked disposable (its comment is {marker!r}). Mark a database you "
            f"created for tests with: COMMENT ON DATABASE {database} IS '{DISPOSABLE_MARKER}';"
        )
    loopback_addr = server_addr is None or server_addr.split("/")[0] in {"127.0.0.1", "::1"} \
        or server_addr.startswith("127.")
    if not loopback_addr and approved.host not in approved.extra_hosts:
        raise UnsafeTestDatabaseError(
            f"Server address {server_addr} is not loopback and {approved.host!r} was not explicitly allowed."
        )
    return {"database": database, "server_addr": server_addr, "server_version": version, "marker": marker}


async def guarded_truncate(
    session: Any, approved: ApprovedTestDatabase | None, statement: str, engine_url: URL,
) -> None:
    """
    The ONLY route by which the suite wipes tables. Every refusal happens before `statement` is executed; with
    `approved is None` (missing/invalid configuration) not even the verification query is sent.
    """
    if approved is None:
        raise UnsafeTestDatabaseError(
            f"No approved test database is configured ({ENV_URL}); refusing to run destructive test fixtures."
        )
    check_engine_matches(engine_url, approved)
    await verify_connected_database(session, approved)
    await session.execute(text(statement))


# ----------------------------------------------------------------------------------------------------------
# Scratch databases (used by the migration tests). Created and dropped only under the guard's rules.
# ----------------------------------------------------------------------------------------------------------
_CREATED_THIS_PROCESS: set[str] = set()


def _admin_connection(approved: ApprovedTestDatabase):
    import psycopg2

    conn = psycopg2.connect(
        host=approved.host, port=approved.port, user=approved.username,
        password=make_url(approved.async_url).password, dbname="postgres",
    )
    conn.autocommit = True
    return conn


def scratch_urls(approved: ApprovedTestDatabase, name: str) -> tuple[str, str]:
    base = make_url(approved.async_url).set(database=name)
    return (base.render_as_string(hide_password=False),
            base.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False))


def create_scratch_database(
    approved: ApprovedTestDatabase, *, marked: bool = True, template: str | None = None,
) -> str:
    """Create `treasury_test_tmp_<random>` on the APPROVED server (never elsewhere) and optionally mark it."""
    name = f"{SCRATCH_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    if not _SAFE_NAME.match(name) or (template is not None and not _SAFE_NAME.match(template)):
        raise UnsafeTestDatabaseError("Refusing to build a CREATE DATABASE statement from an unsafe name.")
    conn = _admin_connection(approved)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else ""))
            if marked:
                cur.execute(f"COMMENT ON DATABASE \"{name}\" IS '{DISPOSABLE_MARKER}'")
    finally:
        conn.close()
    _CREATED_THIS_PROCESS.add(name)
    return name


def mark_database_disposable(approved: ApprovedTestDatabase, name: str) -> None:
    if not name.startswith(SCRATCH_DB_PREFIX) or not _SAFE_NAME.match(name):
        raise UnsafeTestDatabaseError(f"Refusing to mark {name!r}: not a scratch database.")
    conn = _admin_connection(approved)
    try:
        with conn.cursor() as cur:
            cur.execute(f"COMMENT ON DATABASE \"{name}\" IS '{DISPOSABLE_MARKER}'")
    finally:
        conn.close()


def drop_scratch_database(approved: ApprovedTestDatabase, name: str) -> None:
    """Drops only a database that carries the scratch prefix AND was created by this process or is marked."""
    if not name.startswith(SCRATCH_DB_PREFIX) or not _SAFE_NAME.match(name):
        raise UnsafeTestDatabaseError(f"Refusing to drop {name!r}: not a scratch database.")
    conn = _admin_connection(approved)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s", (name,))
            found = cur.fetchone()
            if found is None:
                return
            if name not in _CREATED_THIS_PROCESS and found[0] != DISPOSABLE_MARKER:
                raise UnsafeTestDatabaseError(f"Refusing to drop {name!r}: not created here and not marked disposable.")
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        conn.close()
    _CREATED_THIS_PROCESS.discard(name)


def sweep_orphan_scratch_databases(approved: ApprovedTestDatabase) -> list[str]:
    """Remove scratch databases left by an earlier crashed run. Only prefixed AND marked databases qualify."""
    conn = _admin_connection(approved)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s AND shobj_description(oid, 'pg_database') = %s",
                (SCRATCH_DB_PREFIX + "%", DISPOSABLE_MARKER),
            )
            orphans = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    for orphan in orphans:
        drop_scratch_database(approved, orphan)
    return orphans


def with_database(approved: ApprovedTestDatabase, name: str) -> ApprovedTestDatabase:
    """The approval of a scratch database on the same approved server (used to run verify_connected_database on it)."""
    async_url, sync_url = scratch_urls(approved, name)
    return replace(approved, async_url=async_url, sync_url=sync_url, name=name)
