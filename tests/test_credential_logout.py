import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db import AsyncSessionLocal
from app.locks import database_lock
from app.models import UpstreamCredential
from app.oauth_manual import (
    get_active_upstream_credential,
    logout_upstream_credentials,
    refresh_upstream_credential,
)
from app.utils import utcnow

AUTH = {"Authorization": "Bearer test-admin-token-with-enough-entropy"}


async def seed_credentials(count=1, expired=False):
    async with AsyncSessionLocal() as db:
        rows = [
            UpstreamCredential(
                provider="codex",
                access_token=f"access-{i}",
                refresh_token=f"refresh-{i}",
                account_id=f"account-{i}",
                expires_at=utcnow() + timedelta(seconds=-1 if expired else 3600),
            )
            for i in range(count)
        ]
        db.add_all(rows)
        await db.commit()
        return [row.id for row in rows]


async def test_http_logout_preserves_rows_and_hides_all_credentials(
    client, monkeypatch
):
    ids = await seed_credentials(2)
    monkeypatch.setattr("app.main.codex_auth_manager.login_status", lambda: {})
    assert (await client.post("/admin/auth/codex/logout", headers=AUTH)).json() == {
        "ok": True,
        "deleted_credentials": 2,
    }
    assert (await client.post("/admin/auth/codex/logout", headers=AUTH)).json()[
        "deleted_credentials"
    ] == 0
    async with AsyncSessionLocal() as db:
        rows = (
            await db.scalars(select(UpstreamCredential).order_by(UpstreamCredential.id))
        ).all()
        assert [row.id for row in rows] == ids
        assert all(row.deleted_at is not None for row in rows)
        assert [row.access_token for row in rows] == ["access-0", "access-1"]
        assert [row.refresh_token for row in rows] == ["refresh-0", "refresh-1"]
        assert await get_active_upstream_credential(db) is None
    assert (await client.get("/admin/auth/codex/account", headers=AUTH)).json() == {
        "logged_in": False
    }
    status = (await client.get("/admin/auth/upstream", headers=AUTH)).json()
    assert status["latest_credential"] is None
    assert status["resolved"]["available"] is False
    assert (await client.get("/readyz")).status_code == 503


async def test_cli_logout_and_status_follow_same_soft_delete(capsys):
    import manage

    await seed_credentials(2)
    await manage.cmd_logout(argparse.Namespace())
    assert json.loads(capsys.readouterr().out)["deleted_credentials"] == 2
    await manage.cmd_auth_status(argparse.Namespace(session_id=None))
    assert json.loads(capsys.readouterr().out)["current_credential"] is None
    await manage.cmd_doctor(argparse.Namespace())
    assert json.loads(capsys.readouterr().out)["counts"]["upstream_credentials"] == 0
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(UpstreamCredential.id))) == 2


async def test_relogin_creates_new_credential_without_reviving_history(
    client, monkeypatch
):
    old_id = (await seed_credentials())[0]
    await client.post("/admin/auth/codex/logout", headers=AUTH)

    async def exchange(code, verifier):
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "account_id": "new-account",
            "expires_in": 3600,
        }

    monkeypatch.setattr("app.oauth_manual.exchange_code_for_token", exchange)
    session = (await client.post("/admin/auth/codex/oauth/start", headers=AUTH)).json()
    response = await client.post(
        "/admin/auth/codex/oauth/complete",
        headers=AUTH,
        json={
            "session_id": session["session_id"],
            "callback_url": f"http://localhost:1455/auth/callback?code=test&state={session['state']}",
        },
    )
    assert response.status_code == 200
    assert response.json()["credential_id"] != old_id
    async with AsyncSessionLocal() as db:
        old = await db.get(UpstreamCredential, old_id)
        assert old.deleted_at is not None
        assert old.access_token == "access-0"
        current = await get_active_upstream_credential(db)
        assert current.access_token == "new-access"
        assert current.deleted_at is None
        assert await db.scalar(select(func.count(UpstreamCredential.id))) == 2


async def test_logout_wins_before_waiting_refresh_acquires_lock(monkeypatch):
    await seed_credentials(expired=True)
    waiting, proceed = asyncio.Event(), asyncio.Event()
    refresher = None

    @asynccontextmanager
    async def delayed_lock(*args, **kwargs):
        if asyncio.current_task() is refresher:
            waiting.set()
            await proceed.wait()
        async with database_lock(*args, **kwargs):
            yield

    async def forbidden_refresh(payload):
        pytest.fail("logged-out credential must not be refreshed")

    monkeypatch.setattr("app.oauth_manual.database_lock", delayed_lock)
    monkeypatch.setattr("app.oauth_manual.post_oauth", forbidden_refresh)

    async def resolve():
        async with AsyncSessionLocal() as db:
            return await get_active_upstream_credential(db)

    refresher = asyncio.create_task(resolve())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        async with AsyncSessionLocal() as db:
            assert await logout_upstream_credentials(db) == 1
        proceed.set()
        assert await asyncio.wait_for(refresher, 1) is None
    finally:
        if not refresher.done():
            refresher.cancel()
            await asyncio.gather(refresher, return_exceptions=True)


async def test_logout_after_inflight_refresh_still_deletes_credential(monkeypatch):
    await seed_credentials(expired=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(payload):
        entered.set()
        await release.wait()
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr("app.oauth_manual.post_oauth", refresh)

    async def resolve():
        async with AsyncSessionLocal() as db:
            return await get_active_upstream_credential(db)

    async def logout():
        async with AsyncSessionLocal() as db:
            return await logout_upstream_credentials(db)

    resolver = asyncio.create_task(resolve())
    logout_task = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        logout_task = asyncio.create_task(logout())
        await asyncio.sleep(0)
        assert not logout_task.done()
        release.set()
        await asyncio.wait_for(resolver, 1)
        assert await asyncio.wait_for(logout_task, 1) == 1
        async with AsyncSessionLocal() as db:
            assert await get_active_upstream_credential(db) is None
            row = await db.scalar(select(UpstreamCredential))
            assert row.access_token == "new-access"
            assert row.deleted_at is not None
    finally:
        for task in [resolver, logout_task]:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_deleted_credential_cannot_be_refreshed_directly(monkeypatch):
    ids = await seed_credentials(expired=True)
    async with AsyncSessionLocal() as db:
        await logout_upstream_credentials(db)
        credential = await db.get(UpstreamCredential, ids[0])
        with pytest.raises(HTTPException) as error:
            await refresh_upstream_credential(db, credential)
        assert error.value.status_code == 401
