import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.responses import StreamingResponse

from app.db import AsyncSessionLocal
from app.downstream_limits import claim_user
from app.models import ApiKey, User, UserRequest
from app.utils import sha256_text, utcnow


async def test_default_two_concurrent_and_live_update(key):
    first, second = await claim_user(key.user_id), await claim_user(key.user_id)
    third = None
    try:
        with pytest.raises(HTTPException) as exc:
            await claim_user(key.user_id)
        assert exc.value.status_code == 429
        assert exc.value.headers["Retry-After"] == "1"
        async with AsyncSessionLocal() as db:
            user = await db.get(User, key.user_id)
            user.max_concurrency = 3
            await db.commit()
        third = await claim_user(key.user_id)
    finally:
        await first.release()
        await second.release()
        if third:
            await third.release()
    lease = await claim_user(key.user_id)
    await lease.release()


async def test_rpm_interval_and_crash_recovery(key, monkeypatch):
    clock = [utcnow()]
    monkeypatch.setattr("app.downstream_limits.utcnow", lambda: clock[0])
    async with AsyncSessionLocal() as db:
        user = await db.get(User, key.user_id)
        user.max_rpm = 1
        db.add(
            UserRequest(
                id="abandoned",
                user_id=key.user_id,
                started_at=clock[0] - timedelta(hours=2),
                active=True,
            )
        )
        await db.commit()
    lease = await claim_user(key.user_id)
    await lease.release()
    with pytest.raises(HTTPException) as exc:
        await claim_user(key.user_id)
    assert exc.value.headers["Retry-After"] == "60"
    clock[0] += timedelta(seconds=61)
    lease = await claim_user(key.user_id)
    await lease.release()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, key.user_id)
        user.max_rpm = 0
        user.min_interval_seconds = 10
        await db.commit()
    with pytest.raises(HTTPException) as exc:
        await claim_user(key.user_id)
    assert exc.value.headers["Retry-After"] == "10"
    clock[0] += timedelta(seconds=11)
    lease = await claim_user(key.user_id)
    await lease.release()


@pytest.mark.parametrize("stream", [False, True])
async def test_all_keys_share_limits_through_complete_response(
    client, key, monkeypatch, stream
):
    entered = 0
    ready, release = asyncio.Event(), asyncio.Event()
    async with AsyncSessionLocal() as db:
        db.add(
            ApiKey(
                user_id=key.user_id,
                name="second-key",
                key_hash=sha256_text("second-key"),
                key_preview="second",
            )
        )
        await db.commit()

    async def hold():
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await release.wait()

    async def forward(*args, **kwargs):
        await hold()
        return 200, {}, None, None

    async def streaming(*args, **kwargs):
        async def body():
            yield "data: {}\n\n"
            await hold()

        return StreamingResponse(body(), media_type="text/event-stream")

    monkeypatch.setattr("app.main.forward_to_upstream", forward)
    monkeypatch.setattr("app.main.stream_to_upstream", streaming)

    async def request(token):
        return await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + token},
            json={"model": "gpt-test", "input": "hello", "stream": stream},
        )

    first = asyncio.create_task(request("gtw_test"))
    second = asyncio.create_task(request("second-key"))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        rejected = await request("second-key")
        assert rejected.status_code == 429, rejected.text
        assert rejected.headers["retry-after"] == "1"
        assert entered == 2
    finally:
        release.set()
        results = await asyncio.gather(first, second)
    assert all(response.status_code == 200 for response in results)
    assert (await request("gtw_test")).status_code == 200
    async with AsyncSessionLocal() as db:
        rows = (await db.scalars(select(UserRequest))).all()
        assert not any(row.active for row in rows)


async def test_cancellation_releases_slot(client, key, monkeypatch):
    started = asyncio.Event()

    async def forward(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.main.forward_to_upstream", forward)
    task = asyncio.create_task(
        client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer gtw_test"},
            json={"model": "gpt-test", "input": "hello"},
        )
    )
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    async with AsyncSessionLocal() as db:
        rows = (await db.scalars(select(UserRequest))).all()
        assert rows and not any(row.active for row in rows)


async def test_capacity_shared_across_processes_and_recovers_after_crash(key):
    import subprocess
    import sys

    code = """
import asyncio, sys
from app.downstream_limits import claim_user
from app.db import engine
async def run():
    lease = await claim_user(int(sys.argv[1]))
    print('ready', flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await lease.release()
    await engine.dispose()
asyncio.run(run())
"""
    children = []
    try:
        for _ in range(2):
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(key.user_id)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            children.append(process)
            assert (
                await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5)
                == "ready\n"
            )
        with pytest.raises(HTTPException) as exc:
            await claim_user(key.user_id)
        assert exc.value.status_code == 429
        children[0].kill()
        await asyncio.to_thread(children[0].wait)
        lease = await claim_user(key.user_id)
        await lease.release()
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
