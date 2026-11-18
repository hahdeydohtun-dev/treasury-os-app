"""
Stage 5G migration a7c1e9b4d2f6 (adds `reconciliation_adaptive_scores`), tested on disposable scratch databases.

Same fail-closed approach as the Stage 5E migration test: a template database is migrated to the PREVIOUS head and seeded once,
each test clones it, `alembic` runs in a SUBPROCESS that is first asked which database it resolves to (and refuses anything but
the scratch database), and every scratch database is dropped afterwards. Nothing here can reach the application database.
"""
import uuid

import psycopg2
import pytest

from tests._db_safety import (
    create_scratch_database,
    drop_scratch_database,
    sweep_orphan_scratch_databases,
)
from tests.test_migration_accepted_claims_preflight import SEEDS, History, ScratchDb, _approved

PREVIOUS_HEAD = "d5e508d02a28"
NEW_HEAD = "a7c1e9b4d2f6"
TABLE = "reconciliation_adaptive_scores"
# the tables whose rows the migration must not change in any way (Stage 5A-5F data)
PROTECTED = ("reconciliation_runs", "reconciliation_match_suggestions", "reconciliation_match_decisions", "reconciliation_accepted_claims",
             "reconciliation_open_items", "reconciliation_configurations", "bank_statement_transactions", "treasury_transactions")


@pytest.fixture(scope="module")
def template_db():
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
    approved = _approved()
    name = create_scratch_database(approved, template=template_db.name)
    try:
        yield ScratchDb(approved, name)
    finally:
        drop_scratch_database(approved, name)


def _fingerprint(db):
    return {t: db.scalar(f"select coalesce(md5(string_agg(x::text, ',' order by x.id)), 'empty') from {t} x") for t in PROTECTED}


def _count(db, t):
    return db.scalar(f"select count(*) from {t}")


def _artifacts(db):
    return {
        "revision": db.scalar("select version_num from alembic_version"),
        "table": db.scalar(f"select count(*) from information_schema.tables where table_name = '{TABLE}'"),
        "triggers": db.scalar(f"select count(*) from pg_trigger where tgrelid = to_regclass('{TABLE}') and not tgisinternal") if
        db.scalar(f"select to_regclass('{TABLE}') is not null") else 0,
        "functions": db.scalar("select count(*) from pg_proc where proname like 'reconciliation_adaptive_scores_%%'"),
    }


def _history(db):
    """Real Stage 5E-shaped history written with plain SQL: two runs, an accepted and a rejected suggestion, a group."""
    h = History(db)
    run_a, run_b = h.run(), h.run()
    accepted, rejected = h.suggestion(run_a, 0, "ACCEPTED"), h.suggestion(run_b, 1, "REJECTED")
    h.group(run_a, "ONE_TO_MANY", [2, 3], "ACCEPTED")
    h.suggestion(run_b, 4, "PENDING", decide=False)
    return h, run_a, run_b, accepted, rejected


def _insert_score(db, suggestion_id, run_id, h, **over):
    v = {"entity": str(h.entity), "account": str(h.account), "contribution": "0", "recommendation": "80", "band": "AUTO", "fallback": False,
         "reason": None, "support": "NONE", "accepted": 0, **over}
    db.rows(
        f"insert into {TABLE}(id,suggestion_id,reconciliation_run_id,legal_entity_id,bank_account_id,currency_code,direction,match_type,"
        "deterministic_score,adaptive_contribution,recommendation_score,score_band,fallback,fallback_reason,support_level,accepted_examples,"
        "rejected_examples,learning_rule_version,base_rule_version,evidence_cutoff,settings_snapshot,evidence) "
        "values (%s,%s,%s,%s,%s,'NGN','CREDIT','EXACT',80,%s,%s,%s,%s,%s,%s,%s,0,'5G-1.0','5C-1.0',now(),'{}'::jsonb,'[]'::jsonb)",
        (str(uuid.uuid4()), suggestion_id, run_id, v["entity"], v["account"], v["contribution"], v["recommendation"], v["band"],
         v["fallback"], v["reason"], v["support"], v["accepted"]))


# ======================================================================================
def test_upgrade_adds_only_the_new_table_and_leaves_every_existing_row_untouched(clone):
    _h, *_ = _history(clone)
    before, tables_before = _fingerprint(clone), {r[0] for r in clone.rows("select table_name from information_schema.tables where table_schema = 'public'")}
    assert all(_count(clone, t) >= 0 for t in PROTECTED) and _count(clone, "reconciliation_match_decisions") == 3     # not a vacuous check
    assert _artifacts(clone)["table"] == 0 and _artifacts(clone)["revision"] == PREVIOUS_HEAD
    result = clone.run("upgrade", "head")
    assert result.returncode == 0, result.stderr[-1500:]
    assert _artifacts(clone) == {"revision": NEW_HEAD, "table": 1, "triggers": 2, "functions": 2}
    assert _fingerprint(clone) == before                                                                                # not one existing row changed
    assert {r[0] for r in clone.rows("select table_name from information_schema.tables where table_schema = 'public'")} - tables_before == {TABLE}
    assert _count(clone, TABLE) == 0
    assert {c[0] for c in clone.rows(f"select conname from pg_constraint where conrelid = '{TABLE}'::regclass")} >= {
        "uq_reconciliation_adaptive_score_suggestion", "ck_adaptive_score_band", "ck_adaptive_score_fallback_is_deterministic",
        "ck_adaptive_score_contribution_ceiling"}


def test_downgrade_removes_everything_it_added_and_a_re_upgrade_is_clean(clone):
    h, run_a, _run_b, accepted, _rej = _history(clone)
    assert clone.run("upgrade", "head").returncode == 0
    _insert_score(clone, accepted, run_a, h)
    assert _count(clone, TABLE) == 1
    before = _fingerprint(clone)
    down = clone.run("downgrade", "-1")
    assert down.returncode == 0, down.stderr[-1500:]
    assert _artifacts(clone) == {"revision": PREVIOUS_HEAD, "table": 0, "triggers": 0, "functions": 0}              # table, triggers, functions all gone
    assert _fingerprint(clone) == before                                                                            # Stage 5A-5F data is intact
    again = clone.run("upgrade", "head")
    assert again.returncode == 0, again.stderr[-1500:]
    assert _artifacts(clone) == {"revision": NEW_HEAD, "table": 1, "triggers": 2, "functions": 2} and _count(clone, TABLE) == 0


def test_the_database_itself_enforces_immutability_scope_and_constraints(clone):
    h, run_a, run_b, accepted, rejected = _history(clone)
    assert clone.run("upgrade", "head").returncode == 0
    _insert_score(clone, accepted, run_a, h)                                                                         # a valid row is accepted
    assert _count(clone, TABLE) == 1
    for sql in (f"update {TABLE} set adaptive_contribution = 5", f"update {TABLE} set fallback_reason = 'edited'"):   # (DELETE is covered in the scoring tests)
        with pytest.raises(psycopg2.Error, match="immutable"):
            clone.rows(sql)
    assert clone.scalar(f"select adaptive_contribution from {TABLE}") == 0 and _count(clone, TABLE) == 1
    # scope: the entity, bank account and run must be the suggestion's own
    other_entity = clone.scalar("select id from legal_entities where id <> %s order by id limit 1", (str(h.entity),))
    with pytest.raises(psycopg2.Error, match="scope"):
        _insert_score(clone, rejected, run_b, h, entity=str(other_entity))
    with pytest.raises(psycopg2.Error, match="scope"):
        _insert_score(clone, rejected, run_a, h)                                                                     # right entity, WRONG run for this suggestion
    # one explanation per suggestion
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert_score(clone, accepted, run_a, h)
    # impossible values are refused
    for over, constraint in (({"contribution": "16"}, "ck_adaptive_score_contribution_ceiling"), ({"recommendation": "101"}, "ck_adaptive_score_recommendation_range"),
                             ({"band": "MAYBE"}, "ck_adaptive_score_band"), ({"support": "HUGE"}, "ck_adaptive_score_support_level"),
                             ({"accepted": -1}, "ck_adaptive_score_example_counts"), ({"fallback": True, "contribution": "1"}, "ck_adaptive_score_fallback_is_deterministic")):
        with pytest.raises(psycopg2.errors.CheckViolation, match=constraint):
            _insert_score(clone, rejected, run_b, h, **over)
    assert _count(clone, TABLE) == 1


def test_the_migration_chain_has_a_single_head_that_follows_the_previous_one(clone):
    heads = clone.run("heads")
    assert heads.returncode == 0 and NEW_HEAD in heads.stdout and heads.stdout.count("(head)") == 1
    history = clone.run("history", "-r", f"{PREVIOUS_HEAD}:{NEW_HEAD}")
    assert history.returncode == 0 and f"{PREVIOUS_HEAD} -> {NEW_HEAD}" in history.stdout
