import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import AsyncSessionLocal
from app.models import UpstreamCredential
from app.oauth_manual import get_active_upstream_credential
from app.utils import utcnow

ADMIN = {"Authorization": "Bearer test-admin-token-with-enough-entropy"}


async def login(client, account, name=None):
    session = (await client.post("/admin/auth/codex/oauth/start", headers=ADMIN)).json()
    return await client.post(
        "/admin/auth/codex/oauth/complete",
        headers=ADMIN,
        json={
            "session_id": session["session_id"],
            "callback_url": f"http://localhost:1455/auth/callback?code={account}&state={session['state']}",
            "name": name,
        },
    )


@pytest.fixture
async def accounts():
    async with AsyncSessionLocal() as db:
        rows = [
            UpstreamCredential(
                provider="codex",
                name=f"account-{i}",
                account_id=f"account-{i}",
                access_token=f"token-{i}",
                refresh_token=f"refresh-{i}",
                expires_at=utcnow() + timedelta(hours=1),
            )
            for i in range(2)
        ]
        db.add_all(rows)
        await db.commit()
        return [row.id for row in rows]


async def test_login_keeps_different_accounts_and_updates_same_account(
    client, monkeypatch
):
    count = 0

    async def exchange(code, verifier):
        nonlocal count
        count += 1
        return {
            "account_id": code,
            "access_token": f"access-secret-{count}",
            "refresh_token": f"refresh-secret-{count}",
            "expires_in": 3600,
        }

    monkeypatch.setattr("app.oauth_manual.exchange_code_for_token", exchange)
    first = await login(client, "first", "Work")
    second = await login(client, "second", "Personal")
    again = await login(client, "first", "Updated Work")
    assert first.status_code == second.status_code == again.status_code == 200
    assert first.json()["credential_id"] == again.json()["credential_id"]
    assert first.json()["credential_id"] != second.json()["credential_id"]
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(UpstreamCredential.id))) == 2
        work = await db.get(UpstreamCredential, first.json()["credential_id"])
        personal = await db.get(UpstreamCredential, second.json()["credential_id"])
        assert work.name == "Updated Work"
        assert work.access_token == "access-secret-3"
        assert personal.access_token == "access-secret-2"
    listed = await client.get("/admin/upstreams", headers=ADMIN)
    assert len(listed.json()) == 2
    assert "secret" not in listed.text
    assert "refresh_token" not in listed.text


async def test_account_management_binding_and_targeted_logout(client, accounts, key):
    first, second = accounts
    assert (
        await client.patch(
            f"/admin/keys/{key.id}/upstream",
            headers=ADMIN,
            json={"upstream_credential_id": first},
        )
    ).status_code == 200
    keys = (await client.get("/admin/keys", headers=ADMIN)).json()
    assert keys[0]["upstream_credential_id"] == first
    assert (
        await client.patch(
            f"/admin/upstreams/{first}",
            headers=ADMIN,
            json={"enabled": False, "name": "paused"},
        )
    ).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
    assert (
        await client.delete(f"/admin/upstreams/{first}", headers=ADMIN)
    ).status_code == 200
    remaining = (await client.get("/admin/upstreams", headers=ADMIN)).json()
    assert [row["id"] for row in remaining] == [second]
    assert (
        len(
            (
                await client.get("/admin/upstreams?include_deleted=true", headers=ADMIN)
            ).json()
        )
        == 2
    )
    assert (
        await client.patch(
            f"/admin/keys/{key.id}/upstream",
            headers=ADMIN,
            json={"upstream_credential_id": first},
        )
    ).status_code == 404
    assert (
        await client.patch(
            f"/admin/keys/{key.id}/upstream",
            headers=ADMIN,
            json={"upstream_credential_id": None},
        )
    ).status_code == 200


async def test_different_accounts_refresh_independently(accounts, monkeypatch):
    async with AsyncSessionLocal() as db:
        for cid in accounts:
            account = await db.get(UpstreamCredential, cid)
            account.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    refreshing = set()

    async def refresh(payload):
        refreshing.add(payload["refresh_token"])
        if len(refreshing) == 2:
            entered.set()
        await release.wait()
        return {"access_token": "new-token", "expires_in": 3600}

    monkeypatch.setattr("app.oauth_manual.post_oauth", refresh)

    async def resolve(cid):
        async with AsyncSessionLocal() as db:
            return await get_active_upstream_credential(db, cid)

    tasks = [asyncio.create_task(resolve(cid)) for cid in accounts]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert [row.id for row in results] == accounts
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
