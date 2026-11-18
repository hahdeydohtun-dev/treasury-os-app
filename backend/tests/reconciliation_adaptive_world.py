"""
Builders for the Stage 5G tests (not a test module).

History is created through the REAL Stage 5E decision API (accept / reject), so decisions, claims and audit rows are produced by
production code. Every decision's pair follows ONE recurring pattern across all four signal families:
  settlement lag +1 day | bank reference 'NIP26nnnnnnn' vs ledger 'INV-nnnnn' | payee 'acme ltd' | exact amount
so a target pair built the same way has the same four signal keys.
"""
import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.reconciliation import MatchType
from tests.test_reconciliation_acceptance_conflicts import AUTO_REASON, _accept, _run, _suggestion
from tests.test_reconciliation_match_decisions import _decide
from tests.test_reconciliation_matching_engine import _make_bank_statement_txn, _make_treasury_txn

REJECT = "Customer confirmed this receipt belongs to a different invoice."
PATTERN = {"narration": "NIP transfer from acme ltd", "payee": "Acme Ltd"}


async def make_pair(db_session, entity, account, *, i, bank_offset=1, ledger_date="2026-06-10", amount="1000", gap="0", currency="NGN",
                    entry="CREDIT", direction="INFLOW", payee=PATTERN["payee"], narration=PATTERN["narration"], bank_ref=None, ledger_ref=None):
    ledger_day = datetime.date.fromisoformat(ledger_date)
    ledger = await _make_treasury_txn(
        db_session, entity_id=entity.id, account_id=account.id, currency=currency, event_date=str(ledger_day), direction=direction,
        amount=amount, reference=ledger_ref or f"INV-{i:05d}", counterparty=payee)
    bank = await _make_bank_statement_txn(
        db_session, entity_id=entity.id, account_id=account.id, currency=currency, txn_date=str(ledger_day + datetime.timedelta(days=bank_offset)),
        entry_type=entry, amount=str(Decimal(amount) + Decimal(gap)), bank_reference=bank_ref or f"NIP26{i:07d}", narration=f"{narration} {i}")
    return bank, ledger


async def seed_history(client, db_session, headers, entity, account, *, accepted, rejected=0, runs=2, start=0, confidence="95",
                       match_type="EXACT", **pair_kw):
    """`accepted` explicit ACCEPT decisions then `rejected` explicit REJECT decisions, spread over `runs` COMPLETED runs."""
    run_objs = [await _run(db_session, entity, account) for _ in range(runs)]
    made = []
    for n in range(accepted + rejected):
        bank, ledger = await make_pair(db_session, entity, account, i=start + n, **pair_kw)
        suggestion = await _suggestion(db_session, run_objs[n % runs], bank, ledger, confidence=confidence)
        suggestion.match_type = MatchType[match_type]
        made.append((suggestion, n < accepted))
    await db_session.commit()
    for suggestion, ok in made:
        resp = await (_accept(client, headers, "SUGGESTION", str(suggestion.id)) if ok
                      else _decide(client, headers, "SUGGESTION", str(suggestion.id), "reject", REJECT))
        assert resp.status_code == 200, resp.text
    return {"run_ids": [r.id for r in run_objs], "suggestion_ids": [s.id for s, _ in made]}


async def target(db_session, entity, account, *, count=1, confidence="80", reason=AUTO_REASON, start=900, **pair_kw):
    """A NEW run holding `count` PENDING suggestions whose pairs follow the pattern (so they have the same signal keys)."""
    run = await _run(db_session, entity, account)
    suggestions = []
    for n in range(count):
        bank, ledger = await make_pair(db_session, entity, account, i=start + n, **pair_kw)
        suggestions.append(await _suggestion(db_session, run, bank, ledger, confidence=confidence, reason=reason))
    await db_session.commit()
    return {"run": run, "suggestions": suggestions}


async def enabled_config(db_session, entity, *, account=None, adaptive=None, version=1, extra=None, **fields):
    from app.models.reconciliation import ReconciliationConfiguration

    rule = {"adaptive": {"enabled": True, **(adaptive or {})}, **(extra or {})}
    cfg = ReconciliationConfiguration(
        legal_entity_id=entity.id, bank_account_id=account.id if account else None, currency_code=None, amount_tolerance_pct=Decimal(0),
        date_tolerance_days=0, matching_rule_config=rule, is_active=True, version=version, is_current=True,
        effective_from=datetime.date(2026, 1, 1), **fields)
    db_session.add(cfg)
    await db_session.commit()
    return cfg


async def annotate(db_session, run, config, user, **kw):
    from app.services.reconciliation_adaptive_service import annotate_run_adaptively

    result = await annotate_run_adaptively(db_session, run, config, user.test_id, **kw)
    await db_session.commit()
    return result


async def scores(db_session, run_id=None):
    from app.models.reconciliation_adaptive import ReconciliationAdaptiveScore as A

    stmt = select(A).order_by(A.created_at, A.id).execution_options(populate_existing=True)   # refresh rows without expiring other objects
    if run_id is not None:
        stmt = stmt.where(A.reconciliation_run_id == run_id)
    return list((await db_session.execute(stmt)).scalars().all())
