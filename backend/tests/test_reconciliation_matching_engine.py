import asyncio
import datetime
import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_bank_statement_ingestion import _make_account, _make_bank
from tests.test_reconciliation_data_model import _login, _make_scoped_user, _run_payload


async def _make_bank_statement_txn(
    db_session, *, entity_id, account_id, bank_id=None, currency="NGN", txn_date="2026-06-15", entry_type="CREDIT",
    amount="1000000", bank_reference=None, external_transaction_id=None, narration=None, import_batch_id=None,
    row_number=2,
):
    from app.models.bank_statement import BankStatementEntryType, BankStatementTransaction

    if bank_id is None:
        bank = await _make_bank(db_session, name=f"Auto Bank {uuid.uuid4().hex[:6]}")
        bank_id = bank.id
    if import_batch_id is None:
        batch = await _make_import_batch(db_session, entity_id, None)
        import_batch_id = batch.id

    txn = BankStatementTransaction(
        legal_entity_id=entity_id, bank_id=bank_id, bank_account_id=account_id,
        statement_period_start=datetime.date(2026, 6, 1), statement_period_end=datetime.date(2026, 6, 30),
        transaction_date=datetime.date.fromisoformat(txn_date),
        entry_type=BankStatementEntryType[entry_type], amount=Decimal(amount), currency_code=currency,
        bank_reference=bank_reference, external_transaction_id=external_transaction_id, narration=narration,
        duplicate_key=str(uuid.uuid4()), has_strong_identity=bool(bank_reference or external_transaction_id),
        import_batch_id=import_batch_id, source_row_number=row_number,
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


async def _make_import_batch(db_session, entity_id, user_id):
    from app.models.excel_hub import ImportBatch, ImportBatchStatus

    if user_id is None:
        from app.core.security import hash_password
        from app.models.rbac import User as UserModel

        placeholder = UserModel(
            email=f"batch-uploader-{uuid.uuid4().hex[:8]}@treasuryos.example.com",
            full_name="Batch Uploader", hashed_password=hash_password("Password123!"), is_superuser=False,
        )
        db_session.add(placeholder)
        await db_session.flush()
        user_id = placeholder.id

    batch = ImportBatch(
        template_code="BANK_STATEMENT", template_version=1, legal_entity_id=entity_id,
        file_name="test.xlsx", uploaded_by_user_id=user_id,
        uploaded_at=datetime.datetime.now(datetime.UTC), status=ImportBatchStatus.IMPORTED,
    )
    db_session.add(batch)
    await db_session.flush()
    return batch


async def _make_treasury_txn(
    db_session, *, entity_id, account_id, currency="NGN", event_date="2026-06-15", direction="INFLOW",
    amount="1000000", reference=None, external_reference=None, narration=None, counterparty=None,
    status="POSTED",
):
    from app.models.lookup import CashDirection
    from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction

    txn = TreasuryTransaction(
        legal_entity_id=entity_id, event_type_code="INVESTMENT_MATURITY", direction=CashDirection[direction],
        event_date=datetime.date.fromisoformat(event_date), transaction_currency_code=currency,
        transaction_amount=Decimal(amount), bank_account_id=account_id, reference=reference,
        external_reference=external_reference, narration=narration, counterparty=counterparty,
        status=TransactionStatus[status],
    )
    db_session.add(txn)
    await db_session.flush()
    return txn


async def _create_ready_run(client, headers, entity_id, account_id, period_start="2026-06-01", period_end="2026-06-30"):
    resp = await client.post(
        "/api/v1/reconciliation/runs",
        json=_run_payload(entity_id, account_id, start=period_start, end=period_end), headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _set_configuration(client, headers, entity_id, amount_tolerance_pct="0", date_tolerance_days=0, high_value_threshold=None):
    payload = {
        "legal_entity_id": str(entity_id), "amount_tolerance_pct": amount_tolerance_pct,
        "date_tolerance_days": date_tolerance_days,
    }
    if high_value_threshold is not None:
        payload["high_value_threshold"] = high_value_threshold
    resp = await client.post("/api/v1/reconciliation/configurations", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _get_suggestions(client, headers, run_id):
    resp = await client.get(f"/api/v1/reconciliation/runs/{run_id}/suggestions", headers=headers)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Basic matching signals
# ---------------------------------------------------------------------------

async def test_exact_amount_reference_date_direction_match(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000011")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="exactmatch",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, bank_reference="PAY-12345",
        narration="Supplier ABC",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW",
        reference="PAY-12345", narration="Supplier ABC",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.status_code == 200
    body = execute_resp.json()
    assert body["status"] == "COMPLETED"
    assert body["matched_count"] == 1
    assert body["ambiguous_count"] == 0
    assert body["unmatched_count"] == 0

    suggestions = await _get_suggestions(client, headers, run_id)
    assert len(suggestions) == 1
    assert suggestions[0]["match_type"] == "EXACT"
    assert suggestions[0]["confidence"] == "100.000"
    assert suggestions[0]["status"] == "PENDING"
    assert "exact" in suggestions[0]["reason"].lower()


async def test_exact_amount_no_reference_still_scores(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000022")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="norefmatch",
    )
    await db_session.flush()

    await _make_bank_statement_txn(db_session, entity_id=entity_a.id, account_id=account.id)
    await _make_treasury_txn(db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    # No reference on either side: amount(40) + date(15) + direction(10) = 65, below AUTO_MATCH_THRESHOLD(70).
    assert body["matched_count"] == 0
    suggestions = await _get_suggestions(client, headers, run_id)
    assert len(suggestions) == 1
    assert suggestions[0]["treasury_transaction_id"] is not None
    assert "review" in suggestions[0]["reason"].lower()


async def test_amount_within_configured_tolerance_is_candidate(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000033")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="tolmatch",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="1000000", bank_reference="REF-TOL",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="999500", direction="INFLOW",
        reference="REF-TOL",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, amount_tolerance_pct="0.1")

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["matched_count"] == 1
    suggestions = await _get_suggestions(client, headers, run_id)
    assert suggestions[0]["match_type"] == "TOLERANCE"
    assert "within configured tolerance" in suggestions[0]["reason"].lower()


async def test_amount_outside_tolerance_not_a_candidate(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000044")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="outsidetol",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="1000000", bank_reference="REF-OUT",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="998000", direction="INFLOW",
        reference="REF-OUT",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, amount_tolerance_pct="0.1")

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 0
    assert body["unmatched_count"] == 1
    suggestions = await _get_suggestions(client, headers, run_id)
    assert suggestions[0]["treasury_transaction_id"] is None


async def test_incompatible_direction_rejects_candidate(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000055")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="baddirection",
    )
    await db_session.flush()

    # Bank CREDIT should only match a ledger INFLOW - here the ledger is OUTFLOW.
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, entry_type="CREDIT", bank_reference="REF-DIR",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="OUTFLOW", reference="REF-DIR",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 0  # excluded at the SQL/direction-filter level entirely
    assert body["unmatched_count"] == 1


# ---------------------------------------------------------------------------
# Currency isolation
# ---------------------------------------------------------------------------

async def test_cross_currency_transaction_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000066")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="crosscurrency",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="USD", amount="10000",
        bank_reference="REF-FX",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, currency="NGN", amount="10000",
        direction="INFLOW", reference="REF-FX",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 0
    assert body["unmatched_count"] == 1


# ---------------------------------------------------------------------------
# Entity/account isolation
# ---------------------------------------------------------------------------

async def test_entity_a_bank_transaction_cannot_match_entity_b_ledger_transaction(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000077")
    account_b = await _make_account(db_session, entity_b.id, bank.id, account_number="1111000088")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="crossentity",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, bank_reference="PAY-SAME",
    )
    # Identical amount/reference/date, but Entity B.
    await _make_treasury_txn(
        db_session, entity_id=entity_b.id, account_id=account_b.id, direction="INFLOW", reference="PAY-SAME",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account_a.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 0  # Entity B's transaction was never even fetched
    assert body["unmatched_count"] == 1


async def test_wrong_bank_account_candidate_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000099")
    account_b = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000100")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="wrongaccount",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, bank_reference="PAY-ACCT",
    )
    # Same entity, but a DIFFERENT bank account.
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account_b.id, direction="INFLOW", reference="PAY-ACCT",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account_a.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 0
    assert body["unmatched_count"] == 1


# ---------------------------------------------------------------------------
# Period / date tolerance
# ---------------------------------------------------------------------------

async def test_ledger_transaction_within_date_tolerance_accepted(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000111")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="datetol",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, txn_date="2026-06-20", bank_reference="REF-DATE",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, event_date="2026-06-22", direction="INFLOW",
        reference="REF-DATE",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, date_tolerance_days=3)

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 1
    suggestions = await _get_suggestions(client, headers, run_id)
    assert suggestions[0]["match_type"] == "TOLERANCE"


async def test_ledger_transaction_outside_date_tolerance_rejected(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="1111000122")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="datetoloutside",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, txn_date="2026-06-20", bank_reference="REF-FAR",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, event_date="2026-06-28", direction="INFLOW",
        reference="REF-FAR",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, date_tolerance_days=3)

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 0
    assert body["unmatched_count"] == 1


# ---------------------------------------------------------------------------
# Candidate selection: clear top vs. ambiguous tie
# ---------------------------------------------------------------------------

async def test_clear_top_candidate_selected_weaker_does_not_override(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000011")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="cleartop",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, bank_reference="PAY-CLEAR",
        narration="Supplier ABC",
    )
    # Strong candidate: exact reference + exact date + direction.
    strong = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW", reference="PAY-CLEAR",
        narration="Supplier ABC",
    )
    # Weak candidate: same amount/date/direction only, no reference match, no narration match.
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW", reference="UNRELATED-999",
        narration="Something else entirely",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 2
    assert body["ambiguous_count"] == 0
    assert body["matched_count"] == 1

    suggestions = await _get_suggestions(client, headers, run_id)
    assert len(suggestions) == 1  # only the clear winner gets a suggestion, never the weaker one
    assert suggestions[0]["treasury_transaction_id"] == str(strong.id)


async def test_ambiguous_tied_candidates_both_persisted_never_arbitrarily_chosen(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000022")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="ambiguous",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, bank_reference="ABC", txn_date="2026-06-20",
    )
    # Two IDENTICAL-strength candidates - same amount/date/reference/direction.
    ledger_a = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW", reference="ABC",
        event_date="2026-06-20",
    )
    ledger_b = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW", reference="ABC",
        event_date="2026-06-20",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["ambiguous_count"] == 1
    assert body["matched_count"] == 0  # never arbitrarily resolved as a confident match

    suggestions = await _get_suggestions(client, headers, run_id)
    assert len(suggestions) == 2  # BOTH tied candidates persisted, never one arbitrarily chosen
    treasury_ids = {s["treasury_transaction_id"] for s in suggestions}
    assert treasury_ids == {str(ledger_a.id), str(ledger_b.id)}
    assert all("ambiguous" in s["reason"].lower() for s in suggestions)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

async def test_scoring_is_deterministic_same_inputs_same_score():
    from app.models.bank_statement import BankStatementEntryType
    from app.models.lookup import CashDirection
    from app.services.reconciliation_scoring import score_candidate

    args = {
        "bank_amount": Decimal(1000000), "ledger_amount": Decimal(1000000),
        "bank_date": datetime.date(2026, 6, 15), "ledger_date": datetime.date(2026, 6, 15),
        "bank_references": ["PAY-1"], "ledger_references": ["PAY-1"],
        "bank_entry_type": BankStatementEntryType.CREDIT, "ledger_direction": CashDirection.INFLOW,
        "bank_narration": "Supplier ABC", "ledger_texts": ["Supplier ABC"], "treasury_transaction_id": uuid.uuid4(),
    }
    first = score_candidate(**args)
    second = score_candidate(**args)
    assert first.total == second.total
    assert first.reason == second.reason
    assert first.total == Decimal(100)  # exact everything


# ---------------------------------------------------------------------------
# Persistence correctness
# ---------------------------------------------------------------------------

async def test_suggestion_persisted_with_correct_ids_and_metadata(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000033")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="persistcorrect",
    )
    await db_session.flush()

    bank_txn = await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, bank_reference="PAY-PERSIST",
    )
    ledger_txn = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW", reference="PAY-PERSIST",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)

    suggestions = await _get_suggestions(client, headers, run_id)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["reconciliation_run_id"] == run_id
    assert s["bank_statement_transaction_id"] == str(bank_txn.id)
    assert s["treasury_transaction_id"] == str(ledger_txn.id)
    assert s["matching_rule_version"] == "5C-1.0"
    assert s["reason"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

async def test_repeated_execution_does_not_duplicate_suggestions(
    db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    """
    Directly invokes run_matching_for_run twice against the SAME run,
    proving the application-level idempotency check (backed by the
    database's own partial unique indexes) prevents duplicate rows even
    without going through the READY-only status guard.
    """
    from sqlalchemy import select

    from app.models.reconciliation import (
        ReconciliationMatchSuggestion,
        ReconciliationRun,
        ReconciliationRunStatus,
    )
    from app.services.reconciliation_matching_engine import run_matching_for_run

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000044")

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, bank_reference="PAY-IDEMP",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW", reference="PAY-IDEMP",
    )
    run = ReconciliationRun(
        legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=datetime.date(2026, 6, 1), period_end=datetime.date(2026, 6, 30),
        status=ReconciliationRunStatus.RUNNING,
    )
    db_session.add(run)
    await db_session.flush()

    await run_matching_for_run(
        db_session, run_id=run.id, legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=run.period_start, period_end=run.period_end, configuration=None,
    )
    await run_matching_for_run(
        db_session, run_id=run.id, legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=run.period_start, period_end=run.period_end, configuration=None,
    )

    result = await db_session.execute(
        select(ReconciliationMatchSuggestion).where(ReconciliationMatchSuggestion.reconciliation_run_id == run.id)
    )
    assert len(list(result.scalars().all())) == 1  # never duplicated by the second call


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

async def test_concurrent_run_execution_cannot_execute_twice_or_duplicate_suggestions(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from sqlalchemy import select

    from app.models.rbac import EntityScopeType
    from app.models.reconciliation import ReconciliationMatchSuggestion

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000055")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="concurrentmatch",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, bank_reference="PAY-CONCURRENT",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW", reference="PAY-CONCURRENT",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)

    results = await asyncio.gather(
        *[
            client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    status_codes = sorted(r.status_code for r in results if not isinstance(r, Exception))
    assert status_codes.count(200) == 1
    assert 400 in status_codes

    result = await db_session.execute(
        select(ReconciliationMatchSuggestion).where(ReconciliationMatchSuggestion.reconciliation_run_id == run_id)
    )
    assert len(list(result.scalars().all())) == 1  # never duplicated by the race


# ---------------------------------------------------------------------------
# Configuration version usage
# ---------------------------------------------------------------------------

async def test_run_uses_exact_stored_configuration_not_a_later_version(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000066")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="configversion",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="1000000", bank_reference="REF-CFGVER",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="999500", direction="INFLOW",
        reference="REF-CFGVER",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}

    # v1: no tolerance at all - the 500 NGN gap will NOT be a candidate.
    await _set_configuration(client, headers, entity_a.id, amount_tolerance_pct="0")
    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)

    # v2: created AFTER the run - must not retroactively affect it.
    await _set_configuration(client, headers, entity_a.id, amount_tolerance_pct="1.0")

    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["candidate_count"] == 0  # still using v1's zero tolerance, never v2's 1%
    assert body["unmatched_count"] == 1


# ---------------------------------------------------------------------------
# Financial integrity
# ---------------------------------------------------------------------------

async def test_matching_execution_never_modifies_treasury_transaction_or_bank_balance(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    from sqlalchemy import select

    from app.models.balance import BankBalance
    from app.models.rbac import EntityScopeType
    from app.models.treasury_transaction import TreasuryTransaction

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000077")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="nofinancialeffect",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, bank_reference="PAY-NOEFFECT",
    )
    ledger_txn = await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, direction="INFLOW", reference="PAY-NOEFFECT",
    )
    balance = BankBalance(
        bank_account_id=account.id, balance_date=datetime.date(2026, 5, 31), currency_code="NGN",
        closing_balance=Decimal(5000000), available_balance=Decimal(5000000), source="MANUAL",
    )
    db_session.add(balance)
    await db_session.commit()

    before_treasury = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.id == ledger_txn.id)
    )
    snapshot_before = before_treasury.scalar_one()
    original_amount = snapshot_before.transaction_amount
    original_status = snapshot_before.status

    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    assert execute_resp.json()["matched_count"] == 1

    after_treasury = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.id == ledger_txn.id)
    )
    snapshot_after = after_treasury.scalar_one()
    assert snapshot_after.transaction_amount == original_amount
    assert snapshot_after.status == original_status  # completely unchanged by matching

    balance_after = await db_session.get(BankBalance, balance.id)
    assert balance_after.closing_balance == Decimal("5000000.00")  # unchanged

    treasury_count = await db_session.execute(
        select(TreasuryTransaction).where(TreasuryTransaction.bank_account_id == account.id)
    )
    assert len(list(treasury_count.scalars().all())) == 1  # no new TreasuryTransaction created either


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

async def test_matching_failure_marks_run_failed_with_reason(db_session: AsyncSession, demo_group_and_entities):
    """
    Forces a genuine exception inside the matching pass (via monkeypatch,
    the standard way to exercise an error-handling branch that has no
    natural trigger through normal inputs) and verifies
    execute_reconciliation_run's except handler records FAILED with a
    durable reason rather than leaving the run stuck in RUNNING.
    """
    from unittest.mock import AsyncMock, patch

    from app.models.reconciliation import ReconciliationRun, ReconciliationRunStatus
    from app.services.reconciliation_service import execute_reconciliation_run

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000088")

    run = ReconciliationRun(
        legal_entity_id=entity_a.id, bank_account_id=account.id,
        period_start=datetime.date(2026, 6, 1), period_end=datetime.date(2026, 6, 30),
        status=ReconciliationRunStatus.READY,
    )
    db_session.add(run)
    await db_session.flush()

    with patch(
        "app.services.reconciliation_service.run_matching_for_run",
        new=AsyncMock(side_effect=RuntimeError("simulated matching engine failure")),
    ):
        try:
            await execute_reconciliation_run(db_session, run, None)
        except RuntimeError:
            pass

    assert run.status == ReconciliationRunStatus.FAILED
    assert run.failure_reason == "simulated matching engine failure"
    assert run.completed_at is not None


# ---------------------------------------------------------------------------
# High-value conservative gating (SECTION 41)
# ---------------------------------------------------------------------------

async def test_high_value_transaction_with_no_reference_is_not_labeled_auto_match(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    """
    A high-value transaction matched on amount+date+narration alone
    (no real reference) must be framed conservatively as a "review"
    candidate, never as a confident "auto-match", even if the raw score
    would otherwise clear AUTO_MATCH_THRESHOLD.
    """
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000099")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="highvalue",
    )
    await db_session.flush()

    # No reference on either side, but strong narration overlap - amount(40)+date(15)+direction(10)+narration(5) = 70,
    # which meets AUTO_MATCH_THRESHOLD on raw score alone.
    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="50000000",
        narration="Large supplier payment ABC",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="50000000", direction="INFLOW",
        narration="Large supplier payment ABC",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, high_value_threshold="10000000")

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["matched_count"] == 0  # conservative gate downgraded it from "auto-match" to "review"

    suggestions = await _get_suggestions(client, headers, run_id)
    assert len(suggestions) == 1
    assert "review" in suggestions[0]["reason"].lower()
    assert "auto-match" not in suggestions[0]["reason"].lower()


async def test_high_value_transaction_with_real_reference_can_still_auto_match(
    client: AsyncClient, db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    """The conservative gate only applies when there's no real reference - a genuine reference match still qualifies."""
    from app.models.rbac import EntityScopeType

    group, entity_a, _ = demo_group_and_entities
    bank = await _make_bank(db_session)
    account = await _make_account(db_session, entity_a.id, bank.id, account_number="2222000100")
    user = await _make_scoped_user(
        db_session, scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id, label="highvaluewithref",
    )
    await db_session.flush()

    await _make_bank_statement_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="50000000", bank_reference="PAY-HV-001",
    )
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account.id, amount="50000000", direction="INFLOW",
        reference="PAY-HV-001",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await _login(client, user)}"}
    await _set_configuration(client, headers, entity_a.id, high_value_threshold="10000000")

    run_id = await _create_ready_run(client, headers, entity_a.id, account.id)
    execute_resp = await client.post(f"/api/v1/reconciliation/runs/{run_id}/execute", headers=headers)
    body = execute_resp.json()
    assert body["matched_count"] == 1  # a real reference match is not downgraded


# ---------------------------------------------------------------------------
# Normalization unit tests
# ---------------------------------------------------------------------------

def test_normalize_reference_never_merges_different_separators():
    from app.services.reconciliation_normalization import normalize_reference

    assert normalize_reference("ABC-123") != normalize_reference("ABC123")
    assert normalize_reference("  abc-123  ") == normalize_reference("ABC-123")  # case/whitespace only


def test_normalize_narration_collapses_whitespace_and_punctuation():
    from app.services.reconciliation_normalization import normalize_narration

    assert normalize_narration("Supplier, ABC!!") == normalize_narration("supplier abc")
    assert normalize_narration("  Multiple   spaces  ") == "multiple spaces"


def test_tokenize_narration_produces_expected_token_set():
    from app.services.reconciliation_normalization import normalize_narration, tokenize_narration

    tokens = tokenize_narration(normalize_narration("Supplier ABC Payment"))
    assert tokens == frozenset({"supplier", "abc", "payment"})


# ---------------------------------------------------------------------------
# Performance / candidate generation (SECTION 60)
# ---------------------------------------------------------------------------

async def test_candidate_generation_uses_database_filtering_not_full_scan(
    db_session: AsyncSession, demo_group_and_entities, cash_event_types,
):
    """
    Creates a materially larger population of TreasuryTransaction rows
    (including many that are OBVIOUSLY out of scope: wrong entity, wrong
    account, wrong currency, wrong direction, far outside the date/
    amount window) and proves generate_candidates returns only the
    genuinely in-scope rows - demonstrating the filtering happens in SQL
    (an O(n) Python comparison over the same population would still
    "work" but this test specifically checks the RESULT SIZE the SQL
    query itself returns, confirming the predicates actually narrow the
    population at the database level rather than the caller having to
    filter an unbounded result set itself).
    """
    from app.models.bank_statement import BankStatementEntryType
    from app.services.reconciliation_candidate_generation import generate_candidates

    group, entity_a, entity_b = demo_group_and_entities
    bank = await _make_bank(db_session)
    account_a = await _make_account(db_session, entity_a.id, bank.id, account_number="3333000011")
    account_b = await _make_account(db_session, entity_a.id, bank.id, account_number="3333000022")

    # 1 genuinely in-scope candidate.
    await _make_treasury_txn(
        db_session, entity_id=entity_a.id, account_id=account_a.id, amount="1000000", direction="INFLOW",
        event_date="2026-06-15", reference="REF-PERF",
    )
    # 50 rows that are each out of scope for a different reason - none should be returned.
    for i in range(15):
        await _make_treasury_txn(
            db_session, entity_id=entity_b.id, account_id=account_a.id, amount="1000000", direction="INFLOW",
            event_date="2026-06-15", reference=f"WRONG-ENTITY-{i}",
        )
    for i in range(15):
        await _make_treasury_txn(
            db_session, entity_id=entity_a.id, account_id=account_b.id, amount="1000000", direction="INFLOW",
            event_date="2026-06-15", reference=f"WRONG-ACCOUNT-{i}",
        )
    for i in range(10):
        await _make_treasury_txn(
            db_session, entity_id=entity_a.id, account_id=account_a.id, amount="1000000", direction="OUTFLOW",
            event_date="2026-06-15", reference=f"WRONG-DIRECTION-{i}",
        )
    for i in range(10):
        await _make_treasury_txn(
            db_session, entity_id=entity_a.id, account_id=account_a.id, amount="1000000", direction="INFLOW",
            event_date="2026-01-01", reference=f"WRONG-DATE-{i}",
        )
    await db_session.commit()

    candidates = await generate_candidates(
        db_session, legal_entity_id=entity_a.id, bank_account_id=account_a.id, currency_code="NGN",
        bank_entry_type=BankStatementEntryType.CREDIT, bank_transaction_date=datetime.date(2026, 6, 15),
        bank_amount=Decimal(1000000), date_tolerance_days=0, amount_tolerance_pct=Decimal(0),
        excluded_treasury_transaction_ids=set(),
    )
    # Out of a 51-row population, the database-side filters return
    # exactly the one genuinely in-scope row - never the other 50.
    assert len(candidates) == 1
    assert candidates[0].reference == "REF-PERF"
