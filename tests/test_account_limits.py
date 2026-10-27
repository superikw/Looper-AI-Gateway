import asyncio
import json
import sys
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.db import AsyncSessionLocal
from app.locks import lock_path
from app.models import UpstreamCredential, UpstreamRequest, UsageLog
from app.services import close_upstream, open_upstream
from app.streaming import stream_to_upstream
from app.upstreams import claim_account
from app.utils import utcnow

ADMIN = {"Authorization": "Bearer test-admin-token-with-enough-entropy"}
AUTH = {"Authorization": "Bearer gtw_test"}
BODY = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
async def account(monkeypatch):
    monkeypatch.setattr(settings, "upstream_capacity_wait_seconds", 0)
    async with AsyncSessionLocal() as db:
        row = UpstreamCredential(
            provider="codex", account_id="limited-account", access_token="secret"
        )
        db.add(row)
        await db.commit()
        return row.id


class HeldStream(httpx.AsyncByteStream):
    def __init__(self):
        self.gate = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
        await self.gate.wait()
        yield b'data: {"type":"response.completed","response":{"id":"resp","output":[],"output_text":"hello","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n'

    async def aclose(self):
        self.closed = True


@pytest.fixture
async def transport(monkeypatch):
    state = {
        "calls": [],
        "hold": False,
        "streams": [],
        "status": 200,
        "exception": None,
        "malformed": False,
    }

    def handle(request):
        state["calls"].append(request.headers["chatgpt-account-id"])
        if state["exception"]:
            raise state["exception"]
        if state["hold"]:
            stream = HeldStream()
            state["streams"].append(stream)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        if state["malformed"]:
            return httpx.Response(200, text="bad json")
        if state["status"] >= 400:
            return httpx.Response(
                state["status"], json={"error": {"message": "upstream failed"}}
            )
        return httpx.Response(
            200,
            json={
                "id": "resp",
                "output": [],
                "output_text": "hello",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        monkeypatch.setattr("app.services.get_http_client", lambda: http)
        yield state


async def active_count(cid):
    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(func.count())
            .select_from(UpstreamRequest)
            .where(
                UpstreamRequest.upstream_credential_id == cid,
                UpstreamRequest.active.is_(True),
            )
        )


async def configure(client, cid, **changes):
    result = await client.patch(f"/admin/upstreams/{cid}", headers=ADMIN, json=changes)
    assert result.status_code == 200, result.text


async def test_default_rpm_uses_rolling_window_and_preserves_audit(
    client, account, key, transport, monkeypatch
):
    now = utcnow()
    clock = [now]
    monkeypatch.setattr("app.upstreams.utcnow", lambda: clock[0])
    await client.patch(
        f"/admin/keys/{key.id}/upstream",
        headers=ADMIN,
        json={"upstream_credential_id": account},
    )
    for i in range(30):
        result = await client.post(
            "/v1/chat/completions", headers=AUTH, json={**BODY, "stream": bool(i % 2)}
        )
        assert result.status_code == 200, result.text
    blocked = await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert len(transport["calls"]) == 30
    assert (await client.get("/readyz")).status_code == 200
    assert await active_count(account) == 0
    async with AsyncSessionLocal() as db:
        saved = await db.get(UpstreamCredential, account)
        assert (saved.max_rpm, saved.max_concurrency, saved.min_interval_seconds) == (
            30,
            4,
            0,
        )
        assert (
            saved.request_count == 30
            and saved.failure_count == 0
            and saved.cooldown_until is None
        )
        logs = (await db.scalars(select(UsageLog))).all()
        assert len(logs) == 31
        assert all(log.upstream_credential_id == account for log in logs)
    # Exactly 60 seconds later, the first window has expired.
    clock[0] = now + timedelta(seconds=60)
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 200
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(UpstreamRequest)) == 1


async def test_four_in_flight_requests_and_dynamic_lowering(
    client, account, key, transport
):
    transport["hold"] = True
    responses = []

    async def open_one():
        async with AsyncSessionLocal() as db:
            response = await open_upstream("/v1/chat/completions", BODY, db, account)
            responses.append(response)
            return response

    try:
        await asyncio.gather(*(open_one() for _ in range(4)))
        assert await active_count(account) == 4
        with pytest.raises(HTTPException) as error:
            await open_one()
        assert error.value.status_code == 429
        await configure(client, account, max_concurrency=1)
        assert await active_count(account) == 4
        assert not any(stream.closed for stream in transport["streams"])
        for response in responses[:3]:
            await close_upstream(response)
        with pytest.raises(HTTPException) as error:
            await open_one()
        assert error.value.status_code == 429
        await configure(client, account, max_concurrency=2)
        await open_one()
        assert await active_count(account) == 2
        assert len(transport["calls"]) == 5
    finally:
        for response in responses:
            await close_upstream(response)
    assert await active_count(account) == 0


async def test_rpm_and_interval_update_without_restart(client, account, monkeypatch):
    clock = [utcnow()]
    monkeypatch.setattr("app.upstreams.utcnow", lambda: clock[0])
    lease = await claim_account(account, set())
    await lease.release()
    await configure(client, account, max_rpm=1)
    with pytest.raises(HTTPException) as error:
        await claim_account(account, set())
    assert error.value.status_code == 429
    await configure(client, account, max_rpm=30, min_interval_seconds=10)
    with pytest.raises(HTTPException) as error:
        await claim_account(account, set())
    assert error.value.headers["Retry-After"] == "10"
    clock[0] += timedelta(seconds=10)
    lease = await claim_account(account, set())
    await lease.release()
    await configure(client, account, min_interval_seconds=0)
    lease = await claim_account(account, set())
    await lease.release()
    listed = (await client.get("/admin/upstreams", headers=ADMIN)).json()[0]
    assert (
        listed["max_rpm"],
        listed["max_concurrency"],
        listed["min_interval_seconds"],
    ) == (30, 4, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_rpm": 0},
        {"max_rpm": True},
        {"max_rpm": None},
        {"max_rpm": "30"},
        {"max_concurrency": 0},
        {"max_concurrency": 1.5},
        {"max_concurrency": False},
        {"min_interval_seconds": -1},
        {"min_interval_seconds": None},
        {"min_interval_seconds": True},
    ],
)
async def test_invalid_limits_are_rejected(client, account, changes):
    result = await client.patch(
        f"/admin/upstreams/{account}", headers=ADMIN, json=changes
    )
    assert result.status_code == 422


async def test_busy_pool_uses_other_account_but_binding_does_not(
    client, account, key, transport
):
    async with AsyncSessionLocal() as db:
        other = UpstreamCredential(
            provider="codex", account_id="other-account", access_token="other-secret"
        )
        db.add(other)
        await db.commit()
    await configure(client, account, max_concurrency=1)
    lease = await claim_account(account, set())
    try:
        assert (
            await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
        ).status_code == 200
        assert transport["calls"] == ["other-account"]
        await client.patch(
            f"/admin/keys/{key.id}/upstream",
            headers=ADMIN,
            json={"upstream_credential_id": account},
        )
        assert (
            await client.post(
                "/v1/chat/completions", headers=AUTH, json={**BODY, "stream": True}
            )
        ).status_code == 429
        assert transport["calls"] == ["other-account"]
    finally:
        await lease.release()


async def test_waiting_request_observes_live_update(
    client, account, key, transport, monkeypatch
):
    monkeypatch.setattr(settings, "upstream_capacity_wait_seconds", 2)
    await configure(client, account, max_concurrency=1)
    lease = await claim_account(account, set())
    task = asyncio.create_task(
        client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    )
    try:
        await asyncio.sleep(0.15)
        assert not task.done() and transport["calls"] == []
        await configure(client, account, max_concurrency=2)
        result = await asyncio.wait_for(task, 2)
        assert result.status_code == 200
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await lease.release()


async def test_wait_timeout_is_bounded(client, account, key, transport, monkeypatch):
    monkeypatch.setattr(settings, "upstream_capacity_wait_seconds", 0.15)
    await configure(client, account, max_concurrency=1)
    lease = await claim_account(account, set())
    try:
        result = await asyncio.wait_for(
            client.post("/v1/chat/completions", headers=AUTH, json=BODY), 1
        )
        assert result.status_code == 429 and int(result.headers["retry-after"]) >= 1
        assert transport["calls"] == []
        assert await active_count(account) == 1
    finally:
        await lease.release()


@pytest.mark.parametrize("cancel", [False, True])
async def test_stream_holds_slot_until_completion_or_cancel(
    account, key, transport, cancel
):
    transport["hold"] = True
    async with AsyncSessionLocal() as db:
        response = await stream_to_upstream(
            "/v1/chat/completions", {**BODY, "stream": True}, db, key
        )
    iterator = response.body_iterator
    await anext(iterator)
    await anext(iterator)
    assert await active_count(account) == 1
    stream = transport["streams"][0]
    if cancel:
        pending = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        stream.gate.set()
        assert "[DONE]" in "".join([chunk async for chunk in iterator])
    await response.background()
    assert stream.closed and await active_count(account) == 0
    async with AsyncSessionLocal() as db:
        logs = (await db.scalars(select(UsageLog))).all()
        assert len(logs) == 1 and logs[0].upstream_credential_id == account
        assert logs[0].status_code == (499 if cancel else 200)


@pytest.mark.parametrize(
    "options,status",
    [
        ({"exception": httpx.ConnectError("failed")}, 502),
        ({"exception": httpx.ReadTimeout("timeout")}, 504),
        ({"status": 400}, 400),
        ({"status": 429}, 429),
        ({"malformed": True}, 502),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_errors_release_slot(
    client, account, key, transport, options, status, stream
):
    transport.update(options)
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**BODY, "stream": stream}
    )
    assert response.status_code == status
    assert await active_count(account) == 0


async def test_long_stream_does_not_expire_its_slot(account, monkeypatch):
    clock = [utcnow()]
    monkeypatch.setattr("app.upstreams.utcnow", lambda: clock[0])
    leases = [await claim_account(account, set()) for _ in range(4)]
    try:
        clock[0] += timedelta(hours=2)
        with pytest.raises(HTTPException) as error:
            await claim_account(account, set())
        assert error.value.status_code == 429
    finally:
        for lease in leases:
            await lease.release()
    old_path = lock_path(f"account-request-{leases[0].id}")
    lease = await claim_account(account, set())
    assert not old_path.exists()
    await lease.release()


async def test_processes_share_concurrency_and_recover_after_crash(account):
    script = """
import asyncio,json,sys
from fastapi import HTTPException
from app.upstreams import claim_account
from app.db import engine
async def run():
    leases=[]
    for _ in range(4):
        try:
            lease=await claim_account(int(sys.argv[1]),set())
            leases.append(lease)
        except HTTPException as error:
            assert error.status_code == 429
    print(json.dumps({'held':len(leases)}),flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    for lease in leases:
        await lease.release()
    await engine.dispose()
asyncio.run(run())
"""
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(account),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(2)
    ]
    lease = None
    try:
        lines = await asyncio.wait_for(
            asyncio.gather(*(process.stdout.readline() for process in processes)), 15
        )
        held = [json.loads(line)["held"] for line in lines]
        assert sum(held) == 4
        with pytest.raises(HTTPException) as error:
            await claim_account(account, set())
        assert error.value.status_code == 429
        crashed = processes[next(i for i, count in enumerate(held) if count)]
        crashed.kill()
        await crashed.wait()
        lease = await claim_account(account, set())
        assert lease.credential_id == account
    finally:
        if lease:
            await lease.release()
        for process in processes:
            if process.returncode is None:
                process.stdin.write(b"\n")
                await process.stdin.drain()
                await asyncio.wait_for(process.communicate(), 5)


async def test_cancellation_before_upstream_headers_releases_reservation(
    account, transport, monkeypatch
):
    entered = asyncio.Event()
    never = asyncio.Event()

    async def send(*args):
        entered.set()
        await never.wait()

    monkeypatch.setattr("app.services._send_upstream", send)

    async def open_one():
        async with AsyncSessionLocal() as db:
            return await open_upstream("/v1/chat/completions", BODY, db, account)

    task = asyncio.create_task(open_one())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await active_count(account) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await active_count(account) == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rpm_is_shared_and_survives_new_processes(account, client):
    await configure(client, account, max_rpm=5)
    script = """
import asyncio,json,sys
from fastapi import HTTPException
from app.upstreams import claim_account
from app.db import engine
async def run():
    claimed=0
    for _ in range(5):
        try:
            lease=await claim_account(int(sys.argv[1]),set())
            await lease.release()
            claimed+=1
        except HTTPException as error:
            assert error.status_code==429
    await engine.dispose()
    print(json.dumps(claimed))
asyncio.run(run())
"""
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(account),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(2)
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(p.communicate() for p in processes)), 15
        )
        counts = []
        for process, (stdout, stderr) in zip(processes, results):
            assert process.returncode == 0, stderr.decode()
            counts.append(json.loads(stdout))
        assert sum(counts) == 5
        with pytest.raises(HTTPException) as error:
            await claim_account(account, set())
        assert error.value.status_code == 429
        await configure(client, account, max_rpm=6)
        lease = await claim_account(account, set())
        await lease.release()
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_slow_refresh_keeps_rpm_reserved_until_actual_send(
    client, account, monkeypatch
):
    clock = [utcnow()]
    monkeypatch.setattr("app.upstreams.utcnow", lambda: clock[0])
    await configure(client, account, max_rpm=1)
    lease = await claim_account(account, set())
    try:
        clock[0] += timedelta(seconds=70)
        with pytest.raises(HTTPException) as error:
            await claim_account(account, set())
        assert error.value.status_code == 429
        await lease.start_request(asyncio.get_running_loop().time())
    finally:
        await lease.release()
    with pytest.raises(HTTPException) as error:
        await claim_account(account, set())
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "60"
    clock[0] += timedelta(seconds=60)
    lease = await claim_account(account, set())
    await lease.release()


async def test_actual_dispatch_keeps_interval_after_refresh_and_history_cleanup(
    client, account, monkeypatch
):
    clock = [utcnow()]
    monkeypatch.setattr("app.upstreams.utcnow", lambda: clock[0])
    lease = await claim_account(account, set())
    clock[0] += timedelta(seconds=70)
    await lease.start_request(asyncio.get_running_loop().time())
    await lease.release()
    # The reservation is already 70 seconds old, but the model call just began.
    await configure(client, account, min_interval_seconds=120)
    clock[0] += timedelta(seconds=90)
    with pytest.raises(HTTPException) as error:
        await claim_account(account, set())
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "30"
    clock[0] += timedelta(seconds=30)
    lease = await claim_account(account, set())
    await lease.release()


async def test_zero_interval_stays_disabled_if_clock_moves_back(account, monkeypatch):
    clock = [utcnow()]
    monkeypatch.setattr("app.upstreams.utcnow", lambda: clock[0])
    lease = await claim_account(account, set())
    await lease.start_request(asyncio.get_running_loop().time())
    await lease.release()
    clock[0] -= timedelta(seconds=1)
    lease = await claim_account(account, set())
    try:
        await lease.start_request(asyncio.get_running_loop().time())
    finally:
        await lease.release()
