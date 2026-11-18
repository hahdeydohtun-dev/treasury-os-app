"""
Automated tests for the accepted-claims migration (revision d5e508d02a28), run against DISPOSABLE PostgreSQL
databases created on the approved test server (see tests/_db_safety.py). Nothing here touches the shared test
database or any other database:

  * a template database is migrated to the PREVIOUS head (f4c533db287d) and seeded once per module;
  * each test clones it (CREATE DATABASE ... TEMPLATE), writes "historical" reconciliation decisions straight into
    the clone with SQL - exactly what the pre-claims code could have produced, since it never checked overlaps -
    and runs `alembic upgrade <the Stage 5E revision under test>` in a SUBPROCESS whose environment points at the clone;
  * every scratch database is dropped afterwards (only prefixed, marked or self-created databases can be dropped),
    and leftovers from a crashed earlier run are swept first, so the tests are safe to rerun.

Before any migration runs, each subprocess is first asked which database its settings resolve to, and the test
aborts unless it is the intended scratch database: the Alembic environment takes its URL from the settings.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy.engine import make_url

from tests._db_safety import (
    DISPOSABLE_MARKER,
    SCRATCH_DB_PREFIX,
    UnsafeTestDatabaseError,
    create_scratch_database,
    drop_scratch_database,
    get_approved_test_database,
    scratch_urls,
    sweep_orphan_scratch_databases,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
PREVIOUS_HEAD = "f4c533db287d"
NEW_HEAD = "d5e508d02a28"
SEEDS = ["seed_reference_data", "seed_forecast_categories", "seed_facility_types", "seed_investment_types", "seed_demo_data"]


def _approved():
    approved = get_approved_test_database()
    assert approved is not None, "an approved disposable test database is required (pytest_configure enforces it)"
    return approved


class ScratchDb:
    """A scratch database plus helpers that can only ever talk to THAT database."""

    def __init__(self, approved, name):
        if not name.startswith(SCRATCH_DB_PREFIX):
            raise UnsafeTestDatabaseError(f"{name!r} is not a scratch database")
        self.approved, self.name = approved, name
        self.async_url, self.sync_url = scratch_urls(approved, name)

    def connect(self):
        url = make_url(self.sync_url)
        conn = psycopg2.connect(host=url.host, port=url.port, user=url.username, password=url.password, dbname=self.name)
        conn.autocommit = True
        return conn

    def rows(self, sql, params=()):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
        finally:
            conn.close()

    def scalar(self, sql, params=()):
        return self.rows(sql, params)[0][0]

    def run(self, *args, module="alembic"):
        """Run `python -m <module> <args>` in a subprocess aimed at THIS scratch database only."""
        env = {**os.environ, "DATABASE_URL": self.async_url, "DATABASE_URL_SYNC": self.sync_url, "PYTHONDONTWRITEBYTECODE": "1"}
        probe = subprocess.run(
            [sys.executable, "-c", "from app.core.config import settings; print(settings.DATABASE_URL_SYNC)"],
            cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=120, check=False,
        )
        resolved = make_url(probe.stdout.strip()).database if probe.returncode == 0 and probe.stdout.strip() else None
        if resolved != self.name:      # fail closed: never run a migration/seed against anything but the scratch DB
            raise UnsafeTestDatabaseError(f"subprocess settings resolve to {resolved!r}, not {self.name!r}; refusing")
        return subprocess.run([sys.executable, "-m", module, *args], cwd=BACKEND_DIR, env=env,
                              capture_output=True, text=True, timeout=300, check=False)


@pytest.fixture(scope="module")
def template_db():
    """Migrated to the previous head and seeded, once. Cloned by every test; dropped at the end of the module."""
    approved = _approved()
    sweep_orphan_scratch_databases(approved)
    name = create_scratch_database(approved)
    db = ScratchDb(approved, name)
    try:
        result = db.run("upgrade", PREVIOUS_HEAD)
        assert result.returncode == 0, result.stderr[-1500:]
        for seed in SEEDS:
            seeded = db.run(module=f"app.db.{seed}")
            assert seeded.returncode == 0, f"{seed}: {seeded.stderr[-800:]}"
        assert db.scalar("select version_num from alembic_version") == PREVIOUS_HEAD
        yield db
    finally:
        drop_scratch_database(approved, name)


@pytest.fixture
def clone(template_db):
    """A fresh disposable copy of the template for ONE test, always dropped afterwards."""
    approved = _approved()
    name = create_scratch_database(approved, template=template_db.name)
    try:
        yield ScratchDb(approved, name)
    finally:
        drop_scratch_database(approved, name)


# --------------------------------------------------------------------------------------------------
# helpers that write "historical" reconciliation history with plain SQL
# --------------------------------------------------------------------------------------------------
class History:
    def __init__(self, db):
        self.db = db
        self.user = db.scalar("select id from users order by id limit 1")
        self.entity, self.account = db.rows("select legal_entity_id, id from bank_accounts order by created_at limit 1")[0]
        self.txns = [r[0] for r in db.rows("select id from treasury_transactions order by id")]
        assert len(self.txns) >= 5, "demo data should provide at least five ledger transactions"

    def run(self):
        run_id = str(uuid.uuid4())
        self.db.rows(
            "insert into reconciliation_runs(id,legal_entity_id,bank_account_id,period_start,period_end,status,"
            "statement_transaction_count,eligible_transaction_count,candidate_count,suggestion_count,matched_count,"
            "ambiguous_count,unmatched_count,advanced_match_count,ambiguous_advanced_count,created_at,updated_at) "
            "values (%s,%s,%s,'2026-06-01','2026-06-30','COMPLETED',0,0,0,0,0,0,0,0,0,now(),now())",
            (run_id, str(self.entity), str(self.account)))
        return run_id

    def decision(self, run_id, *, suggestion=None, group=None, decision="ACCEPTED"):
        self.db.rows(
            "insert into reconciliation_match_decisions(id,reconciliation_run_id,legal_entity_id,kind,suggestion_id,group_id,"
            "decision,previous_status,new_status,reason,decided_by_user_id,created_at,updated_at) "
            "values (%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,'historical',%s,now(),now())",
            (str(uuid.uuid4()), run_id, str(self.entity), "SUGGESTION" if suggestion else "GROUP", suggestion, group,
             decision, decision, str(self.user)))

    def suggestion(self, run_id, txn_index, status="ACCEPTED", decide=True):
        sid = str(uuid.uuid4())
        self.db.rows(
            "insert into reconciliation_match_suggestions(id,reconciliation_run_id,treasury_transaction_id,match_type,"
            "confidence,status,reason,matching_rule_version,created_at,updated_at) "
            "values (%s,%s,%s,'EXACT',95,%s,'x','5C-1.0',now(),now())",
            (sid, run_id, str(self.txns[txn_index]), status))
        if decide and status in ("ACCEPTED", "REJECTED"):
            self.decision(run_id, suggestion=sid, decision=status)
        return sid

    def group(self, run_id, relationship, txn_indexes, status="ACCEPTED", decide=True):
        gid = str(uuid.uuid4())
        self.db.rows(
            "insert into reconciliation_match_groups(id,reconciliation_run_id,relationship_type,status,"
            "bank_aggregate_amount,ledger_aggregate_amount,currency_code,difference,score,confidence,reason,"
            "matching_rule_version,group_key,created_at,updated_at) "
            "values (%s,%s,%s,%s,100,100,'NGN',0,100,100,'historical','5D-1.0',%s,now(),now())",
            (gid, run_id, relationship, status, f"{relationship}:{gid}"))
        for index in txn_indexes:
            self.db.rows("insert into reconciliation_match_group_members(id,match_group_id,treasury_transaction_id,created_at,updated_at) "
                         "values (%s,%s,%s,now(),now())", (str(uuid.uuid4()), gid, str(self.txns[index])))
        if decide and status in ("ACCEPTED", "REJECTED"):
            self.decision(run_id, group=gid, decision=status)
        return gid


def _migration_artifacts(db):
    return {
        "revision": db.scalar("select version_num from alembic_version"),
        "claims_table": db.scalar("select count(*) from information_schema.tables where table_name = 'reconciliation_accepted_claims'"),
        "claim_indexes": db.scalar("select count(*) from pg_indexes where indexname in ('ux_accepted_claim_bank','ux_accepted_claim_ledger')"),
        "claim_enum": db.scalar("select count(*) from pg_type where typname = 'acceptedclaimkind'"),
    }


def _history_state(db):
    return (db.rows("select id::text, status::text from reconciliation_match_suggestions order by id"),
            db.rows("select id::text, status::text from reconciliation_match_groups order by id"),
            db.rows("select id::text, decision::text, new_status from reconciliation_match_decisions order by id"))


# ==================================================================================================
# Scenario 1: conflicting historical accepted decisions -> the migration must abort, changing nothing
# ==================================================================================================
def _two_suggestions_share_a_ledger_row(h):
    h.suggestion(h.run(), 0)
    h.suggestion(h.run(), 0)               # same ledger row accepted again in another run


def _suggestion_and_group_share_a_ledger_row(h):
    h.suggestion(h.run(), 0)
    h.group(h.run(), "ONE_TO_MANY", [0, 1])


def _two_transfer_groups_share_a_leg(h):
    h.group(h.run(), "INTERNAL_TRANSFER", [0, 1])
    h.group(h.run(), "INTERNAL_TRANSFER", [1, 2])


@pytest.mark.parametrize("build", [_two_suggestions_share_a_ledger_row, _suggestion_and_group_share_a_ledger_row,
                                   _two_transfer_groups_share_a_leg])
def test_conflicting_history_aborts_the_migration_before_any_schema_change(clone, build):
    build(History(clone))
    before_state, before_artifacts = _history_state(clone), _migration_artifacts(clone)
    assert before_artifacts == {"revision": PREVIOUS_HEAD, "claims_table": 0, "claim_indexes": 0, "claim_enum": 0}

    result = clone.run("upgrade", NEW_HEAD)

    assert result.returncode != 0
    assert "Cannot add accepted-claim uniqueness" in result.stderr and "already claim the same transaction" in result.stderr
    after = _migration_artifacts(clone)
    assert after == before_artifacts                                    # previous revision kept; nothing created
    assert after["revision"] == PREVIOUS_HEAD and after["claims_table"] == 0 and after["claim_indexes"] == 0
    assert _history_state(clone) == before_state                        # decisions and statuses untouched

    again = clone.run("upgrade", NEW_HEAD)                                # rerunning the refused upgrade is safe and consistent
    assert again.returncode != 0 and _migration_artifacts(clone) == before_artifacts


def test_after_the_operator_resolves_the_conflict_the_same_database_migrates(clone):
    h = History(clone)
    h.suggestion(h.run(), 0)
    second_run = h.run()
    second = h.suggestion(second_run, 0)
    assert clone.run("upgrade", NEW_HEAD).returncode != 0
    clone.rows("delete from reconciliation_match_decisions where suggestion_id = %s", (second,))   # the operator's remedy
    clone.rows("update reconciliation_match_suggestions set status = 'REJECTED' where id = %s", (second,))
    result = clone.run("upgrade", NEW_HEAD)
    assert result.returncode == 0, result.stderr[-1500:]
    assert _migration_artifacts(clone)["revision"] == NEW_HEAD
    assert clone.scalar("select count(*) from reconciliation_accepted_claims") == 1


# ==================================================================================================
# Scenario 2: valid historical decisions -> the migration succeeds and backfills exactly the right claims
# ==================================================================================================
def test_valid_history_migrates_and_backfills_the_expected_claims_without_duplicates(clone):
    h = History(clone)
    run_1, run_2 = h.run(), h.run()
    h.suggestion(run_1, 0)                                   # MATCH on T0
    h.suggestion(run_1, 1)                                   # MATCH on T1
    h.group(run_1, "ONE_TO_MANY", [2, 3])                    # MATCH on T2, T3
    h.group(run_2, "INTERNAL_TRANSFER", [0, 4])              # TRANSFER on T0, T4  (T0 also has a MATCH claim: legal)
    h.suggestion(run_2, 0, status="REJECTED")                # rejected: must NOT claim
    h.suggestion(run_2, 4, status="PENDING", decide=False)   # pending, undecided: must NOT claim
    before_state = _history_state(clone)

    result = clone.run("upgrade", NEW_HEAD)

    assert result.returncode == 0, result.stderr[-1500:]
    assert _migration_artifacts(clone) == {"revision": NEW_HEAD, "claims_table": 1, "claim_indexes": 2, "claim_enum": 1}
    assert clone.scalar("select count(*) from reconciliation_accepted_claims") == 6
    assert dict(clone.rows("select claim_kind::text, count(*) from reconciliation_accepted_claims group by 1")) == {"MATCH": 4, "TRANSFER": 2}
    assert clone.scalar(
        "select count(*) from (select treasury_transaction_id, claim_kind from reconciliation_accepted_claims "
        "group by 1, 2 having count(*) > 1) d") == 0                         # no duplicates
    claimed = {(str(t), k) for t, k in clone.rows("select treasury_transaction_id, claim_kind::text from reconciliation_accepted_claims")}
    expected = {(str(h.txns[i]), "MATCH") for i in (0, 1, 2, 3)} | {(str(h.txns[i]), "TRANSFER") for i in (0, 4)}
    assert claimed == expected
    assert clone.scalar("select count(*) from reconciliation_accepted_claims c join reconciliation_match_decisions d "
                        "on d.id = c.decision_id where d.decision <> 'ACCEPTED'") == 0     # only ACCEPTED decisions claim
    assert _history_state(clone) == before_state                              # the migration rewrote no decision or status


def test_the_required_partial_unique_indexes_exist_and_are_enforced_after_migration(clone):
    h = History(clone)
    h.suggestion(h.run(), 0)
    assert clone.run("upgrade", NEW_HEAD).returncode == 0
    definitions = dict(clone.rows("select indexname, indexdef from pg_indexes where indexname in ('ux_accepted_claim_bank','ux_accepted_claim_ledger')"))
    assert set(definitions) == {"ux_accepted_claim_bank", "ux_accepted_claim_ledger"}
    for name, column in (("ux_accepted_claim_bank", "bank_statement_transaction_id"), ("ux_accepted_claim_ledger", "treasury_transaction_id")):
        d = definitions[name]
        assert "CREATE UNIQUE INDEX" in d and f"({column}, claim_kind)" in d and f"WHERE ({column} IS NOT NULL)" in d

    # the index really is enforced by PostgreSQL: a second MATCH claim on the same ledger row is refused...
    decision_id, entity = clone.rows("select id, legal_entity_id from reconciliation_match_decisions limit 1")[0]
    insert = ("insert into reconciliation_accepted_claims(id,decision_id,legal_entity_id,claim_kind,treasury_transaction_id,created_at,updated_at) "
              "values (%s,%s,%s,%s,%s,now(),now())")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        clone.rows(insert, (str(uuid.uuid4()), str(decision_id), str(entity), "MATCH", str(h.txns[0])))
    # ...while the same row in the other namespace is allowed
    clone.rows(insert, (str(uuid.uuid4()), str(decision_id), str(entity), "TRANSFER", str(h.txns[0])))


def test_empty_history_migrates_cleanly(clone):
    result = clone.run("upgrade", NEW_HEAD)
    assert result.returncode == 0, result.stderr[-1500:]
    assert _migration_artifacts(clone)["revision"] == NEW_HEAD
    assert clone.scalar("select count(*) from reconciliation_accepted_claims") == 0
    assert clone.run("upgrade", NEW_HEAD).returncode == 0          # idempotent
    assert clone.run("heads").returncode == 0


def test_every_scratch_database_is_dropped_after_the_tests(template_db):
    # the template itself is still alive here (module scope); no marked clone may remain from earlier tests
    names = {r[0] for r in template_db.rows(
        "select datname from pg_database where datname like %s and shobj_description(oid, 'pg_database') = %s",
        (SCRATCH_DB_PREFIX + "%", DISPOSABLE_MARKER))}
    assert names == {template_db.name}
