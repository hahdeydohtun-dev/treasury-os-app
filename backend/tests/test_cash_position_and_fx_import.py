import datetime
from decimal import Decimal

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


async def test_cash_position_aggregates_latest_balance_per_account(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_bank_and_account,
):
    from app.models.balance import BankBalance

    bank, account = demo_bank_and_account
    db_session.add_all([
        BankBalance(
            bank_account_id=account.id, balance_date=datetime.date(2026, 1, 1),
            currency_code="NGN", closing_balance=Decimal(100000), source="MANUAL",
        ),
        # A later balance should be the one used in the position, not the earlier one.
        BankBalance(
            bank_account_id=account.id, balance_date=datetime.date(2026, 1, 5),
            currency_code="NGN", closing_balance=Decimal(250000),
            available_balance=Decimal(240000), source="MANUAL",
        ),
    ])
    await db_session.commit()

    token = await _login(client, group_wide_manager)
    resp = await client.get(
        "/api/v1/cash-position",
        params={"legal_entity_id": str(account.legal_entity_id), "as_of": "2026-01-10"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_count"] == 1
    assert body["total_cash"] == "250000.00"
    assert body["available_cash"] == "240000.00"
    assert body["by_currency"]["NGN"] == "250000.00"


async def test_cash_position_as_of_date_excludes_future_balances(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User,
    demo_bank_and_account,
):
    from app.models.balance import BankBalance

    bank, account = demo_bank_and_account
    db_session.add(BankBalance(
        bank_account_id=account.id, balance_date=datetime.date(2026, 3, 1),
        currency_code="NGN", closing_balance=Decimal(999999), source="MANUAL",
    ))
    await db_session.commit()

    token = await _login(client, group_wide_manager)
    resp = await client.get(
        "/api/v1/cash-position",
        params={"bank_account_id": str(account.id), "as_of": "2026-01-01"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["account_count"] == 0
    assert resp.json()["total_cash"] == "0"


async def test_fx_rate_import_via_excel_creates_new_version_not_overwrite(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User, demo_currencies,
):
    from sqlalchemy import select

    from app.models.currency import FXRate

    await db_session.commit()
    token = await _login(client, group_wide_manager)
    headers = {"Authorization": f"Bearer {token}"}

    # First import: establishes version 1
    file_v1 = make_xlsx_bytes(
        ["Rate Date", "From Currency", "To Currency", "Rate", "Rate Type", "Source"],
        [["2026-02-01", "USD", "NGN", "1500", "SPOT", "EXCEL"]],
    )
    up1 = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "FX_RATES", "template_version": "1"},
        files={"file": ("fx1.xlsx", file_v1,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )
    batch1 = up1.json()
    assert batch1["valid_rows"] == 1
    confirm1 = await client.post(f"/api/v1/excel/imports/{batch1['id']}/confirm", headers=headers)
    assert confirm1.json()["imported_rows"] == 1

    # Second import, same identity, corrected rate: must create version 2, not overwrite v1
    file_v2 = make_xlsx_bytes(
        ["Rate Date", "From Currency", "To Currency", "Rate", "Rate Type", "Source"],
        [["2026-02-01", "USD", "NGN", "1510", "SPOT", "EXCEL"]],
    )
    up2 = await client.post(
        "/api/v1/excel/uploads",
        data={"template_code": "FX_RATES", "template_version": "1"},
        files={"file": ("fx2.xlsx", file_v2,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )
    batch2 = up2.json()
    confirm2 = await client.post(f"/api/v1/excel/imports/{batch2['id']}/confirm", headers=headers)
    assert confirm2.json()["imported_rows"] == 1

    result = await db_session.execute(
        select(FXRate).where(
            FXRate.from_currency_code == "USD", FXRate.to_currency_code == "NGN"
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 2
    old_row = next(r for r in rows if r.version == 1)
    new_row = next(r for r in rows if r.version == 2)
    assert old_row.is_current is False
    assert old_row.rate == Decimal("1500.0000000000")
    assert new_row.is_current is True
    assert new_row.rate == Decimal("1510.0000000000")
