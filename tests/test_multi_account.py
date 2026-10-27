import asyncio
import json
import sys
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
    changed = await client.patch(
        f"/admin/upstreams/{first.json()['credential_id']}",
        headers=ADMIN,
        json={"max_rpm": 17, "max_concurrency": 2, "min_interval_seconds": 0.5},
    )
    assert changed.status_code == 200
    again = await login(client, "first", "Updated Work")
    assert first.status_code == second.status_code == again.status_code == 200
    assert first.json()["credential_id"] == again.json()["credential_id"]
    assert first.json()["credential_id"] != second.json()["credential_id"]
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(UpstreamCredential.id))) == 2
        work = await db.get(UpstreamCredential, first.json()["credential_id"])
        personal = await db.get(UpstreamCredential, second.json()["credential_id"])
        assert work.name == "Updated Work"
        assert (work.max_rpm, work.max_concurrency, work.min_interval_seconds) == (
            17,
            2,
            0.5,
        )
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


@pytest.fixture
async def pool_transport(monkeypatch):
    import httpx

    state = {"requests": [], "responses": {}}

    def handle(request):
        account = request.headers["chatgpt-account-id"]
        state["requests"].append(account)
        outcome = state["responses"].get(account)
        if callable(outcome):
            return outcome(request)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return httpx.Response(
                outcome,
                json={"error": {"message": f"HTTP {outcome}"}},
                headers={"retry-after": "120"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "response",
                        "status": "completed",
                        "output_text": account,
                        "output": [],
                        "usage": {
                            "input_tokens": 4,
                            "output_tokens": 2,
                            "total_tokens": 6,
                        },
                    },
                }
            )
            + "\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        monkeypatch.setattr("app.services.get_http_client", lambda: transport)
        yield state


AUTH = {"Authorization": "Bearer gtw_test"}
BODY = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]}


async def test_pool_rotates_and_attributes_usage(client, accounts, key, pool_transport):
    from app.models import ApiKey, UsageLog

    for _ in range(4):
        assert (
            await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
        ).status_code == 200
    assert pool_transport["requests"] == ["account-0", "account-1"] * 2
    async with AsyncSessionLocal() as db:
        rows = (
            await db.scalars(select(UpstreamCredential).order_by(UpstreamCredential.id))
        ).all()
        assert [(row.request_count, row.total_tokens) for row in rows] == [
            (2, 12),
            (2, 12),
        ]
        saved = await db.get(ApiKey, key.id)
        assert (saved.request_count, saved.total_tokens) == (4, 24)
        assert (
            list(
                (
                    await db.scalars(
                        select(UsageLog.upstream_credential_id).order_by(UsageLog.id)
                    )
                ).all()
            )
            == accounts * 2
        )


async def test_rate_limited_account_fails_over_and_enters_cooldown(
    client, accounts, key, pool_transport
):
    from app.models import ApiKey, UsageLog

    pool_transport["responses"]["account-0"] = 429
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "account-1"
    assert pool_transport["requests"] == ["account-0", "account-1"]
    async with AsyncSessionLocal() as db:
        first = await db.get(UpstreamCredential, accounts[0])
        assert first.failure_count == 1
        assert (first.cooldown_until - utcnow()).total_seconds() > 110
        assert (await db.get(ApiKey, key.id)).request_count == 1
        assert (await db.scalar(select(UsageLog))).upstream_credential_id == accounts[1]
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 200
    assert pool_transport["requests"] == ["account-0", "account-1", "account-1"]


async def test_bound_key_does_not_fail_over_and_can_be_unbound(
    client, accounts, key, pool_transport
):
    await client.patch(
        f"/admin/keys/{key.id}/upstream",
        headers=ADMIN,
        json={"upstream_credential_id": accounts[0]},
    )
    for _ in range(2):
        assert (
            await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
        ).status_code == 200
    pool_transport["responses"]["account-0"] = 429
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 429
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 503
    assert pool_transport["requests"] == ["account-0"] * 3
    await client.patch(
        f"/admin/keys/{key.id}/upstream",
        headers=ADMIN,
        json={"upstream_credential_id": None},
    )
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 200
    assert pool_transport["requests"][-1] == "account-1"


@pytest.mark.parametrize("status", [401, 403, 500, 502, 503, 504])
async def test_retryable_http_failures_use_another_account(
    client, accounts, key, pool_transport, status
):
    pool_transport["responses"]["account-0"] = status
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 200
    assert pool_transport["requests"] == ["account-0", "account-1"]


async def test_bad_request_is_not_retried(client, accounts, key, pool_transport):
    pool_transport["responses"]["account-0"] = 400
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 400
    assert pool_transport["requests"] == ["account-0"]


async def test_ambiguous_timeout_is_not_replayed(client, accounts, key, pool_transport):
    import httpx

    pool_transport["responses"]["account-0"] = httpx.ReadTimeout(
        "may have reached upstream"
    )
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 504
    assert pool_transport["requests"] == ["account-0"]


async def test_connection_failure_can_fail_over(client, accounts, key, pool_transport):
    import httpx

    pool_transport["responses"]["account-0"] = httpx.ConnectError("connection refused")
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 200
    assert pool_transport["requests"] == ["account-0", "account-1"]


async def test_refresh_failure_only_cools_down_affected_account(
    client, accounts, key, pool_transport, monkeypatch
):
    from fastapi import HTTPException

    async with AsyncSessionLocal() as db:
        account = await db.get(UpstreamCredential, accounts[0])
        account.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()

    async def refresh(payload):
        raise HTTPException(401, "refresh rejected")

    monkeypatch.setattr("app.oauth_manual.post_oauth", refresh)
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 200
    assert pool_transport["requests"] == ["account-1"]
    async with AsyncSessionLocal() as db:
        assert (await db.get(UpstreamCredential, accounts[0])).failure_count == 1
        assert (await db.get(UpstreamCredential, accounts[1])).cooldown_until is None


async def test_stream_failover_happens_before_output(
    client, accounts, key, pool_transport
):
    from app.models import UsageLog
    from app.protocol import parse_sse_events

    pool_transport["responses"]["account-0"] = 503
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**BODY, "stream": True}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    chunks = parse_sse_events(response.text)
    assert any(
        choice["delta"].get("content") == "account-1"
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )
    assert pool_transport["requests"] == ["account-0", "account-1"]
    async with AsyncSessionLocal() as db:
        assert (await db.scalar(select(UsageLog))).upstream_credential_id == accounts[1]
        assert (await db.get(UpstreamCredential, accounts[1])).total_tokens == 6


async def test_stream_failure_does_not_mix_accounts(
    client, accounts, key, pool_transport
):
    import httpx

    pool_transport["responses"]["account-0"] = lambda request: httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text='data: {"type":"response.output_text.delta","delta":"partial"}\n\ndata: {"type":"response.failed","response":{"status":"failed","error":{"message":"failure"}}}\n\n',
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**BODY, "stream": True}
    )
    assert "partial" in response.text and "failure" in response.text
    assert pool_transport["requests"] == ["account-0"]
    async with AsyncSessionLocal() as db:
        assert (await db.get(UpstreamCredential, accounts[0])).failure_count == 1


async def test_all_cooling_accounts_are_unready_until_reset(
    client, accounts, key, pool_transport
):
    pool_transport["responses"].update({"account-0": 429, "account-1": 429})
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 429
    assert (await client.get("/readyz")).status_code == 503
    assert (
        await client.patch(
            f"/admin/upstreams/{accounts[0]}",
            headers=ADMIN,
            json={"reset_cooldown": True},
        )
    ).status_code == 200
    assert (await client.get("/readyz")).status_code == 200


async def test_claims_are_balanced_under_concurrency_and_persist(accounts):
    from app.db import engine
    from app.upstreams import claim_account

    async def claim_and_release():
        lease = await claim_account(None, set())
        try:
            return lease.credential_id
        finally:
            await lease.release()

    claims = await asyncio.gather(*(claim_and_release() for _ in range(20)))
    assert claims.count(accounts[0]) == claims.count(accounts[1]) == 10
    await engine.dispose()
    lease = await claim_account(None, set())
    assert lease.credential_id == accounts[0]
    await lease.release()


async def test_late_failure_does_not_shorten_rate_limit_cooldown(accounts):
    from app.upstreams import mark_account_failure

    await mark_account_failure(accounts[0], 120)
    await mark_account_failure(accounts[0], 10)
    async with AsyncSessionLocal() as db:
        row = await db.get(UpstreamCredential, accounts[0])
        assert row.failure_count == 2
        assert (row.cooldown_until - utcnow()).total_seconds() > 110


async def test_pool_selection_is_shared_across_processes(accounts):
    script = """
import asyncio, json
from app.upstreams import claim_account
from app.db import engine
async def run():
    claims = []
    for _ in range(6):
        lease = await claim_account(None, set())
        claims.append(lease.credential_id)
        await lease.release()
    await engine.dispose()
    print(json.dumps(claims))
asyncio.run(run())
"""
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(2)
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(process.communicate() for process in processes)), 20
        )
        claims = []
        for process, (stdout, stderr) in zip(processes, results):
            assert process.returncode == 0, stderr.decode()
            claims.extend(json.loads(stdout))
        assert claims.count(accounts[0]) == claims.count(accounts[1]) == 6
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_attempt_limit_and_disabled_binding(
    client, accounts, key, pool_transport, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "upstream_max_attempts", 1)
    pool_transport["responses"]["account-0"] = 503
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 503
    assert pool_transport["requests"] == ["account-0"]
    await client.patch(
        f"/admin/keys/{key.id}/upstream",
        headers=ADMIN,
        json={"upstream_credential_id": accounts[1]},
    )
    await client.patch(
        f"/admin/upstreams/{accounts[1]}",
        headers=ADMIN,
        json={"enabled": False},
    )
    assert (
        await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    ).status_code == 503
    assert pool_transport["requests"] == ["account-0"]


@pytest.mark.parametrize(
    "value, expected",
    [("120", 120), ("garbage", 60), ("nan", 60), ("-1", 1), ("999999", 86400)],
)
def test_retry_after_bounds(value, expected):
    import httpx

    from app.services import cooldown_for_response

    assert (
        cooldown_for_response(httpx.Response(429, headers={"retry-after": value}))
        == expected
    )


def test_retry_after_http_date():
    from datetime import UTC, datetime
    from email.utils import format_datetime

    import httpx

    from app.services import cooldown_for_response

    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert (
        119
        <= cooldown_for_response(httpx.Response(429, headers={"retry-after": future}))
        <= 120
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_responses_uses_pool_and_fixed_binding(
    client, accounts, key, pool_transport, stream
):
    await client.patch(
        f"/admin/keys/{key.id}/upstream",
        headers=ADMIN,
        json={"upstream_credential_id": accounts[1]},
    )
    response = await client.post(
        "/v1/responses",
        headers=AUTH,
        json={"model": "gpt-5.5", "input": "hello", "stream": stream},
    )
    assert response.status_code == 200
    assert pool_transport["requests"] == ["account-1"]
    if stream:
        assert "event: response.completed" in response.text
    else:
        assert response.json()["output_text"] == "account-1"
