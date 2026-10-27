from sqlalchemy import func, select

from app.db import AsyncSessionLocal
from app.models import ApiKey, UsageLog
from app.services import record_usage

AUTH = {"Authorization": "Bearer test-admin-token-with-enough-entropy"}


async def test_stale_sessions_do_not_lose_usage(key):
    async with AsyncSessionLocal() as a, AsyncSessionLocal() as b:
        first = await a.get(ApiKey, key.id)
        second = await b.get(ApiKey, key.id)
        for session, entity in [(a, first), (b, second)]:
            await record_usage(
                session,
                entity,
                "/v1/responses",
                "gpt-5.5",
                200,
                {"usage": {"input_tokens": 3, "output_tokens": 2}},
                None,
            )
    async with AsyncSessionLocal() as db:
        saved = await db.get(ApiKey, key.id)
        assert (
            saved.request_count,
            saved.prompt_tokens,
            saved.completion_tokens,
            saved.total_tokens,
        ) == (2, 6, 4, 10)
        assert await db.scalar(select(func.count(UsageLog.id))) == 2


async def test_delete_used_key_preserves_usage_and_revokes_access(client, key):
    async with AsyncSessionLocal() as db:
        entity = await db.get(ApiKey, key.id)
        await record_usage(db, entity, "/v1/responses", "gpt-5.5", 200, {}, None)
    response = await client.delete(f"/admin/keys/{key.id}", headers=AUTH)
    assert response.status_code == 200
    assert (await client.get("/admin/keys", headers=AUTH)).json() == []
    assert (
        await client.get("/v1/models", headers={"Authorization": "Bearer gtw_test"})
    ).status_code == 401
    assert (
        await client.post(f"/admin/keys/{key.id}/enable", headers=AUTH)
    ).status_code == 404
    summary = (await client.get("/admin/usage/summary", headers=AUTH)).json()
    assert summary["totals"]["request_count"] == 1
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(UsageLog.id))) == 1


async def test_migrates_legacy_database():
    from sqlalchemy import inspect, text

    from app.db import engine
    from app.migrations import initialize_database

    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE api_keys"))
        await conn.execute(text("CREATE TABLE api_keys (id INTEGER PRIMARY KEY)"))
    await initialize_database()
    await initialize_database()
    async with engine.connect() as conn:
        columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("api_keys")}
        )
        assert "deleted_at" in columns


async def test_parallel_usage_writes_all_count(key):
    import asyncio

    async def request():
        async with AsyncSessionLocal() as db:
            await record_usage(
                db,
                key,
                "/v1/responses",
                "gpt-5.5",
                200,
                {"usage": {"input_tokens": 1}},
                None,
            )

    await asyncio.gather(*(request() for _ in range(20)))
    async with AsyncSessionLocal() as db:
        saved = await db.get(ApiKey, key.id)
        assert saved.request_count == 20
        assert saved.prompt_tokens == 20
        assert await db.scalar(select(func.count(UsageLog.id))) == 20
