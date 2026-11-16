"""
Runtime tests for the acceptance eligibility checks: entity, bank account and currency mismatches.

Each defect is built from valid rows - no database constraint is bypassed (every foreign key resolves; the
transactions simply belong to a different entity / account / currency than the run being accepted). For every
defect the refusal must be the documented HTTP 409 and must leave NOTHING behind: no decision, no claim, no
ACCEPTED status, no MATCH_ACCEPTED audit event, and no change to ledger, bank statement or balance records.
"""
import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_acceptance_conflicts import (
    _accept,
    _bank,
    _decisions,
    _group,
    _ledger,
    _run,
    _suggestion,
)
from tests.test_reconciliation_open_items import _headers, _setup

ENTITY_OR_ACCOUNT = "outside this run's legal entity / bank account"


async def _world(client, db_session, demo_group_and_entities, label, number):
    """The run's own entity/account, plus a different account of the SAME entity and an account of ANOTHER entity."""
    from app.models.balance import BankBalance

    _, _, entity_b = demo_group_and_entities
    entity, account, user = await _setup(client, db_session, demo_group_and_entities, label, number)
    other_account = await _make_account(db_session, entity.id, (await _make_bank(db_session)).id,
                                        account_number=number[:-1] + "7")
    foreign_account = await _make_account(db_session, entity_b.id, (await _make_bank(db_session)).id,
                                          account_number=number[:-1] + "8")
    db_session.add(BankBalance(bank_account_id=account.id, balance_date=datetime.date(2026, 5, 31), currency_code="NGN",
                               closing_balance=Decimal(5000000), available_balance=Decimal(5000000), source="MANUAL"))
    await db_session.flush()
    return {"entity": entity, "entity_b": entity_b, "account": account, "other_account": other_account,
            "foreign_account": foreign_account, "user": user}


async def _build(db_session, w, kind, defect):
    """One proposal, valid except for `defect` (None = a fully valid control). Returns (proposal_id, expected phrase)."""
    entity, account = w["entity"], w["account"]
    bank_args, ledger_args = (entity, account), (entity, account)
    ledger_kw: dict = {}
    phrase = ENTITY_OR_ACCOUNT
    if defect == "entity_bank":
        bank_args = (w["entity_b"], w["foreign_account"])
    elif defect == "entity_ledger":
        ledger_args = (w["entity_b"], w["foreign_account"])
    elif defect == "account_bank":
        bank_args = (entity, w["other_account"])
    elif defect == "account_ledger":
        ledger_args = (entity, w["other_account"])
    elif defect == "currency":
        ledger_kw = {"currency": "USD"}
        phrase = "different currencies" if kind == "SUGGESTION" else "does not match the proposal's currency"

    run = await _run(db_session, entity, account)
    if kind == "SUGGESTION":
        bank = await _bank(db_session, *bank_args, "ELIG-S", amount="1000000")
        ledger = await _ledger(db_session, *ledger_args, "ELIG-S", amount="1000000", **ledger_kw)
        return (await _suggestion(db_session, run, bank, ledger)).id, phrase
    bank = await _bank(db_session, *bank_args, "ELIG-G", amount="10000000")
    good = await _ledger(db_session, entity, account, "ELIG-G1", amount="4000000")
    other = await _ledger(db_session, *ledger_args, "ELIG-G2", amount="6000000", **ledger_kw)
    return (await _group(db_session, run, "ONE_TO_MANY", [bank], [good, other])).id, phrase


async def _snapshot(db_session):
    from app.models.audit import AuditEvent
    from app.models.balance import BankBalance
    from app.models.bank_statement import BankStatementTransaction
    from app.models.treasury_transaction import TreasuryTransaction

    db_session.expire_all()
    return (
        (await db_session.execute(select(TreasuryTransaction.id, TreasuryTransaction.legal_entity_id, TreasuryTransaction.bank_account_id,
                                         TreasuryTransaction.transaction_amount, TreasuryTransaction.transaction_currency_code,
                                         TreasuryTransaction.status).order_by(TreasuryTransaction.id))).all(),
        (await db_session.execute(select(BankStatementTransaction.id, BankStatementTransaction.legal_entity_id,
                                         BankStatementTransaction.bank_account_id, BankStatementTransaction.amount,
                                         BankStatementTransaction.currency_code, BankStatementTransaction.status)
                                  .order_by(BankStatementTransaction.id))).all(),
        (await db_session.execute(select(BankBalance.id, BankBalance.closing_balance, BankBalance.available_balance))).all(),
        (await db_session.execute(select(func.count()).select_from(AuditEvent))).scalar_one(),
    )


async def _status(db_session, kind, proposal_id):
    from app.models.reconciliation import ReconciliationMatchGroup, ReconciliationMatchSuggestion

    model = ReconciliationMatchSuggestion if kind == "SUGGESTION" else ReconciliationMatchGroup
    db_session.expire_all()
    return (await db_session.get(model, proposal_id)).status.value


# --------------------------------------------------------------------------------------------------
# positive controls: the same fixtures, without the defect, ARE acceptable
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["SUGGESTION", "GROUP"])
async def test_control_the_defect_free_proposal_is_accepted(client, db_session, demo_group_and_entities, cash_event_types, kind):
    w = await _world(client, db_session, demo_group_and_entities, f"el-ok-{kind[:3].lower()}", "9810000011")
    pid, _ = await _build(db_session, w, kind, None)
    await db_session.commit()
    resp = await _accept(client, await _headers(client, w["user"]), kind, pid)
    assert resp.status_code == 200 and resp.json()["status"] == "ACCEPTED"


# --------------------------------------------------------------------------------------------------
# the three mismatches, for both proposal kinds
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["SUGGESTION", "GROUP"])
@pytest.mark.parametrize("defect", ["entity_bank", "entity_ledger", "account_bank", "account_ledger", "currency"])
async def test_mismatched_proposal_is_refused_with_409_and_leaves_nothing_behind(
    client, db_session, demo_group_and_entities, cash_event_types, kind, defect,
):
    w = await _world(client, db_session, demo_group_and_entities, f"el-{kind[:1].lower()}{defect[:6]}", "9820000011")
    pid, phrase = await _build(db_session, w, kind, defect)
    await db_session.commit()
    headers = await _headers(client, w["user"])
    before = await _snapshot(db_session)

    resp = await _accept(client, headers, kind, pid)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"].startswith("Cannot accept: ") and phrase in resp.json()["detail"]
    assert await _decisions(db_session) == (0, 0, 0)                 # no decision, no claim, no MATCH_ACCEPTED audit event
    assert await _status(db_session, kind, pid) == "PENDING"        # did not become ACCEPTED
    assert await _snapshot(db_session) == before                     # ledger, statements, balances, audit count unchanged

    again = await _accept(client, headers, kind, pid)                # still refused, still nothing written
    assert again.status_code == 409 and await _decisions(db_session) == (0, 0, 0)


async def test_fx_group_whose_ledger_currency_is_not_the_proposals_target_currency_is_refused(
    client, db_session, demo_group_and_entities, cash_event_types,
):
    w = await _world(client, db_session, demo_group_and_entities, "el-fx", "9830000011")
    entity, account = w["entity"], w["account"]
    run = await _run(db_session, entity, account)
    bank = await _bank(db_session, entity, account, "ELIG-FX", amount="1000", currency="USD")
    wrong = await _ledger(db_session, entity, account, "ELIG-FX", amount="1000", currency="USD")   # target must be NGN
    good = await _ledger(db_session, entity, account, "ELIG-FX2", amount="1600000", currency="NGN")
    bad_group, ok_group = await _group(db_session, run, "FX_MATCH", [bank], [wrong]), None
    bank2 = await _bank(db_session, entity, account, "ELIG-FX3", amount="1000", currency="USD", row=3)
    ok_group = await _group(db_session, run, "FX_MATCH", [bank2], [good])
    ids = (bad_group.id, ok_group.id)
    await db_session.commit()
    headers = await _headers(client, w["user"])
    before = await _snapshot(db_session)
    refused = await _accept(client, headers, "GROUP", ids[0])
    assert refused.status_code == 409 and "no longer matches the FX proposal" in refused.json()["detail"]
    assert await _decisions(db_session) == (0, 0, 0) and await _status(db_session, "GROUP", ids[0]) == "PENDING"
    assert await _snapshot(db_session) == before
    assert (await _accept(client, headers, "GROUP", ids[1])).status_code == 200      # control: a correct FX group is fine


async def test_a_mismatched_bank_member_in_a_many_to_one_group_is_refused(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _world(client, db_session, demo_group_and_entities, "el-m2o", "9840000011")
    entity, account = w["entity"], w["account"]
    run = await _run(db_session, entity, account)
    ok_bank = await _bank(db_session, entity, account, "ELIG-M1", amount="4000000")
    foreign_bank = await _bank(db_session, w["entity_b"], w["foreign_account"], "ELIG-M2", amount="6000000", row=3)
    ledger = await _ledger(db_session, entity, account, "ELIG-M", amount="10000000")
    group = await _group(db_session, run, "MANY_TO_ONE", [ok_bank, foreign_bank], [ledger])
    gid = group.id
    await db_session.commit()
    headers = await _headers(client, w["user"])       # before _snapshot(), which expires every ORM object
    before = await _snapshot(db_session)
    resp = await _accept(client, headers, "GROUP", gid)
    assert resp.status_code == 409 and ENTITY_OR_ACCOUNT in resp.json()["detail"]
    assert await _decisions(db_session) == (0, 0, 0) and await _snapshot(db_session) == before


# --------------------------------------------------------------------------------------------------
# retained semantics: an INTERNAL_TRANSFER pair leg may sit in another ACCOUNT, but never another ENTITY
# --------------------------------------------------------------------------------------------------
async def test_transfer_pair_leg_in_another_account_of_the_same_entity_is_still_accepted(client, db_session, demo_group_and_entities, cash_event_types):
    from app.models.reconciliation import AcceptedClaimKind, ReconciliationAcceptedClaim

    w = await _world(client, db_session, demo_group_and_entities, "el-xfer-ok", "9850000011")
    entity, account = w["entity"], w["account"]
    run = await _run(db_session, entity, account)
    out_leg = await _ledger(db_session, entity, account, "XF-OUT", amount="500000", direction="OUTFLOW")
    in_leg = await _ledger(db_session, entity, w["other_account"], "XF-IN", amount="500000", direction="INFLOW")
    gid = (await _group(db_session, run, "INTERNAL_TRANSFER", [], [out_leg, in_leg])).id
    await db_session.commit()
    resp = await _accept(client, await _headers(client, w["user"]), "GROUP", gid)
    assert resp.status_code == 200 and resp.json()["status"] == "ACCEPTED"
    kinds = (await db_session.execute(select(ReconciliationAcceptedClaim.claim_kind))).scalars().all()
    assert sorted(k.value for k in kinds) == [AcceptedClaimKind.TRANSFER.value] * 2        # separate namespace preserved


async def test_transfer_pair_leg_belonging_to_another_entity_is_refused(client, db_session, demo_group_and_entities, cash_event_types):
    w = await _world(client, db_session, demo_group_and_entities, "el-xfer-bad", "9860000011")
    entity, account = w["entity"], w["account"]
    run = await _run(db_session, entity, account)
    out_leg = await _ledger(db_session, entity, account, "XF-OUT2", amount="500000", direction="OUTFLOW")
    foreign_leg = await _ledger(db_session, w["entity_b"], w["foreign_account"], "XF-IN2", amount="500000", direction="INFLOW")
    gid = (await _group(db_session, run, "INTERNAL_TRANSFER", [], [out_leg, foreign_leg])).id
    await db_session.commit()
    headers = await _headers(client, w["user"])
    before = await _snapshot(db_session)
    resp = await _accept(client, headers, "GROUP", gid)
    assert resp.status_code == 409 and ENTITY_OR_ACCOUNT in resp.json()["detail"]
    assert await _decisions(db_session) == (0, 0, 0) and await _snapshot(db_session) == before
