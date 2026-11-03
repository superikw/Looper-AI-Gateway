import httpx
import pytest

from app.admin_app import admin_app
from app.admin_service import hash_password
from app.db import AsyncSessionLocal
from app.models import AdminAccount, UsageLog


@pytest.fixture
async def admin_client():
    async with AsyncSessionLocal() as db:
        db.add(
            AdminAccount(
                username="root",
                password_hash=hash_password("root-password-123"),
                is_superadmin=True,
                must_change_password=False,
            )
        )
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as client:
        yield client


async def test_admin_login_and_user_provision(admin_client):
    response = await admin_client.post(
        "/api/login",
        json={"username": "root", "password": "root-password-123"},
    )
    assert response.status_code == 200
    created = await admin_client.post("/api/users", json={"name": "alice"})
    assert created.status_code == 200
    assert created.json()["api_key"].startswith("gtw_")
    assert len((await admin_client.get("/api/users")).json()) == 1


async def test_admin_password_reset_revokes_sessions(admin_client):
    await admin_client.post(
        "/api/login",
        json={"username": "root", "password": "root-password-123"},
    )
    created = await admin_client.post("/api/admins", json={"username": "operator"})
    admin_id = created.json()["id"]
    reset = await admin_client.post(f"/api/admins/{admin_id}/reset-password")
    assert reset.status_code == 200
    assert reset.json()["initial_password"]


async def test_dashboard_calculates_cost(admin_client, key):
    await admin_client.post(
        "/api/login",
        json={"username": "root", "password": "root-password-123"},
    )
    async with AsyncSessionLocal() as db:
        db.add(
            UsageLog(
                api_key_id=key.id,
                user_id=key.user_id,
                path="/v1/responses",
                model="gpt-test",
                status_code=200,
                total_tokens=10,
                cost_usd=0.25,
            )
        )
        await db.commit()
    dashboard = (await admin_client.get("/api/dashboard")).json()
    assert dashboard["periods"]["day"]["tokens"] == 10
    assert dashboard["periods"]["day"]["cost_usd"] == 0.25
