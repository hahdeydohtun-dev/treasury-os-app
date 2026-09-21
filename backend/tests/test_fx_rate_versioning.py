import datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.currency import Currency, FXRate
from app.models.rbac import User


async def test_fx_rate_update_creates_new_version_not_overwrite(
    client: AsyncClient, db_session: AsyncSession, group_wide_manager: User
):
    db_session.add_all(
        [
            Currency(code="USD", name="US Dollar"),
            Currency(code="NGN", name="Nigerian Naira"),
        ]
    )
    await db_session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": group_wide_manager.email, "password": "Password123!"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "from_currency_code": "USD",
        "to_currency_code": "NGN",
        "rate_type": "SPOT",
        "rate_date": str(datetime.date(2026, 1, 1)),
        "rate": "1500.00",
        "rate_source": "MANUAL",
    }
    first = await client.post("/api/v1/fx-rates", json=payload, headers=headers)
    assert first.status_code == 201
    assert first.json()["version"] == 1

    payload["rate"] = "1510.00"
    second = await client.post("/api/v1/fx-rates", json=payload, headers=headers)
    assert second.status_code == 201
    assert second.json()["version"] == 2

    result = await db_session.execute(
        select(FXRate).where(
            FXRate.from_currency_code == "USD", FXRate.to_currency_code == "NGN"
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 2, "original rate row must still exist, not be overwritten"

    old_row = next(r for r in rows if r.version == 1)
    new_row = next(r for r in rows if r.version == 2)
    assert old_row.is_current is False
    assert old_row.rate == Decimal("1500.00")
    assert new_row.is_current is True
    assert old_row.superseded_by_id == new_row.id
