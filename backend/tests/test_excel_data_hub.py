from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User
from tests.conftest import make_xlsx_bytes


async def _login(client: AsyncClient, user: User) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_templates_are_listed(client: AsyncClient):
    resp = await client.get("/api/v1/excel/templates")
    assert resp.status_code == 200
    codes = {t["code"] for t in resp.json()}
    assert {"BANK_ACCOUNTS", "BANK_BALANCES", "BANK_TRANSACTIONS", "EXPECTED_COLLECTIONS",
            "EXPECTED_PAYMENTS", "FX_RATES", "BANK_CHARGES"}.issubset(codes)


async def test_upload_missing_required_column_fails_structure_validation(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
):
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    # Missing "Closing Balance" required column
    file_bytes = make_xlsx_bytes(
        ["Entity", "Bank", "Account Number", "Balance Date", "Currency"],
        [["Entity A", "Demo Bank", "1000200030", "2026-01-01", "NGN"]],
    )
    resp = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "BANK_BALANCES", "template_version": "1"},
        files={"file": ("balances.xlsx", file_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "FAILED"
    assert any(i["error_code"] == "MISSING_REQUIRED_COLUMN" for i in body["issues"])


async def test_bank_balance_upload_validate_and_import(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_bank_and_account,
):
    bank, account = demo_bank_and_account
    await db_session.commit()

    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    file_bytes = make_xlsx_bytes(
        ["Entity", "Bank", "Account Number", "Balance Date", "Currency", "Closing Balance"],
        [
            ["Entity A", "Demo Bank A", "1000200030", "2026-01-01", "NGN", "500000"],
            # Duplicate of the row above (same account/date)
            ["Entity A", "Demo Bank A", "1000200030", "2026-01-01", "NGN", "500000"],
            # Invalid currency
            ["Entity A", "Demo Bank A", "1000200030", "2026-01-02", "NGNN", "510000"],
            # Unknown account number
            ["Entity A", "Demo Bank A", "9999999999", "2026-01-01", "NGN", "1000"],
        ],
    )
    upload_resp = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "BANK_BALANCES", "template_version": "1"},
        files={"file": ("balances.xlsx", file_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )
    assert upload_resp.status_code == 201
    batch = upload_resp.json()
    assert batch["total_rows"] == 4
    assert batch["valid_rows"] == 1
    assert batch["duplicate_rows"] == 1
    assert batch["invalid_rows"] == 2
    assert batch["status"] == "READY_FOR_IMPORT"

    error_codes = {i["error_code"] for i in batch["issues"]}
    assert "INVALID_CURRENCY" in error_codes
    assert "ACCOUNT_NOT_FOUND" in error_codes
    assert "DUPLICATE_ROW" in error_codes

    # Preview endpoint returns the same detail
    preview_resp = await client.get(f"/api/v1/excel/validation/{batch['id']}", headers=headers)
    assert preview_resp.status_code == 200
    assert preview_resp.json()["total_rows"] == 4

    # Confirm import
    confirm_resp = await client.post(
        f"/api/v1/excel/imports/{batch['id']}/confirm", headers=headers
    )
    assert confirm_resp.status_code == 200
    result = confirm_resp.json()
    assert result["imported_rows"] == 1
    assert result["skipped_rows"] == 3
    assert result["batch"]["status"] == "PARTIALLY_IMPORTED"

    # The one valid balance actually landed in the DB
    list_resp = await client.get(
        "/api/v1/bank-balances", params={"bank_account_id": str(account.id)}, headers=headers,
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1
    assert list_resp.json()[0]["closing_balance"] == "500000.00"

    # Import history shows the batch
    history_resp = await client.get("/api/v1/excel/imports", headers=headers)
    assert history_resp.status_code == 200
    assert any(b["id"] == batch["id"] for b in history_resp.json())


async def test_entity_scoped_user_cannot_upload_for_other_entity(
    client: AsyncClient, db_session: AsyncSession, entity_a_scoped_user: User,
    demo_group_and_entities,
):
    _, entity_a, entity_b = demo_group_and_entities
    await db_session.commit()

    token = await _login(client, entity_a_scoped_user)
    file_bytes = make_xlsx_bytes(
        ["Entity", "Bank", "Account Number", "Balance Date", "Currency", "Closing Balance"],
        [["Entity B", "Some Bank", "1", "2026-01-01", "USD", "100"]],
    )
    resp = await client.post(
        "/api/v1/excel/uploads",
        data={
            "template_code": "BANK_BALANCES", "template_version": "1",
            "legal_entity_id": str(entity_b.id),
        },
        files={"file": ("balances.xlsx", file_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_upload_requires_auth(client: AsyncClient):
    file_bytes = make_xlsx_bytes(["Entity"], [["A"]])
    resp = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "BANK_BALANCES", "template_version": "1"},
        files={"file": ("x.xlsx", file_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 401


async def test_data_freshness_reports_no_data_for_unused_template(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
):
    await db_session.commit()
    token = await _login(client, group_wide_manager)
    resp = await client.get(
        "/api/v1/excel/freshness", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    entries = {e["template_code"]: e for e in resp.json()}
    assert entries["FX_RATES"]["freshness_status"] == "NO_DATA"
