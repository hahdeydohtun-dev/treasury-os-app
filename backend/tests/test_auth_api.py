from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User


async def test_login_success_and_me(client: AsyncClient, superuser: User, db_session: AsyncSession):
    await db_session.commit()

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": superuser.email, "password": "Password123!"},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    me_resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == superuser.email


async def test_login_wrong_password_rejected(client: AsyncClient, superuser: User, db_session: AsyncSession):
    await db_session.commit()

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": superuser.email, "password": "wrong-password"},
    )
    assert resp.status_code == 401


async def test_me_requires_token(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
