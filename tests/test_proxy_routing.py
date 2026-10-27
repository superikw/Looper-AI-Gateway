import asyncio
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import Settings, settings
from app.db import AsyncSessionLocal
from app.http_client import close_http_client, post_oauth
from app.models import UpstreamCredential, UpstreamRequest, UsageLog
from app.utils import utcnow

AUTH = {"Authorization": "Bearer gtw_test"}
BODY = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
async def accounts():
    async with AsyncSessionLocal() as db:
        rows = [
            UpstreamCredential(
                provider="codex",
                account_id=f"proxy-account-{i}",
                access_token=f"access-{i}",
                refresh_token=f"refresh-{i}",
            )
            for i in range(2)
        ]
        db.add_all(rows)
        await db.commit()
        return [row.id for row in rows]


async def assert_no_account_penalty(ids, status):
    async with AsyncSessionLocal() as db:
        rows = list(
            (
                await db.scalars(
                    select(UpstreamCredential).order_by(UpstreamCredential.id)
                )
            ).all()
        )
        assert [row.id for row in rows] == ids
        assert all(
            row.cooldown_until is None and row.failure_count == 0 for row in rows
        )
        assert [row.request_count for row in rows] == [1, 0]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(UpstreamRequest)
                .where(UpstreamRequest.active.is_(True))
            )
            == 0
        )
        logs = list((await db.scalars(select(UsageLog))).all())
        assert len(logs) == 1 and logs[0].status_code == status
        assert logs[0].upstream_credential_id == ids[0]


@pytest.mark.parametrize(
    "url", ["http://warp:1080", "https://warp:443", "http://user:pass@warp:1080", ""]
)
def test_proxy_setting_accepts_http_connect(url):
    assert Settings(UPSTREAM_PROXY_URL=url).upstream_proxy_url == url


@pytest.mark.parametrize(
    "url",
    [
        "socks5://warp:1080",
        "ftp://warp:1080",
        "http://",
        "http://warp:bad",
        "http://warp:99999",
        "http://warp/path",
        "http://warp?secret=x",
    ],
)
def test_proxy_setting_rejects_invalid_routes(url):
    with pytest.raises(ValidationError):
        Settings(UPSTREAM_PROXY_URL=url)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "error,status",
    [
        (httpx.ProxyError("private proxy credentials"), 503),
        (httpx.ConnectError("private proxy credentials"), 503),
        (httpx.ConnectTimeout("private proxy credentials"), 504),
        (httpx.ReadTimeout("private proxy credentials"), 504),
        (httpx.WriteError("private proxy credentials"), 503),
    ],
)
async def test_proxy_transport_failure_does_not_rotate_or_cool_accounts(
    client, key, accounts, monkeypatch, stream, error, status
):
    monkeypatch.setattr(settings, "upstream_proxy_url", "http://warp:1080")
    calls = []

    def fail(request):
        calls.append(request)
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as upstream:
        monkeypatch.setattr("app.services.get_http_client", lambda: upstream)
        response = await client.post(
            "/v1/chat/completions", headers=AUTH, json={**BODY, "stream": stream}
        )
    assert response.status_code == status
    assert (
        int(response.headers["retry-after"])
        == settings.upstream_failure_cooldown_seconds
    )
    assert len(calls) == 1 and "private" not in response.text
    await assert_no_account_penalty(accounts, status)


async def test_explicit_proxy_error_is_shared_without_explicit_setting(
    client, key, accounts, monkeypatch
):
    monkeypatch.setattr(settings, "upstream_proxy_url", "")

    def fail(request):
        raise httpx.ProxyError("CONNECT rejected")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as upstream:
        monkeypatch.setattr("app.services.get_http_client", lambda: upstream)
        response = await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    assert response.status_code == 503
    await assert_no_account_penalty(accounts, 503)


@pytest.mark.parametrize("readiness", [False, True])
async def test_refresh_proxy_failure_keeps_credentials_and_stops_pool_retry(
    client, key, accounts, monkeypatch, readiness
):
    monkeypatch.setattr(settings, "upstream_proxy_url", "http://warp:1080")
    async with AsyncSessionLocal() as db:
        for cid in accounts:
            credential = await db.get(UpstreamCredential, cid)
            credential.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ConnectError("proxy down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as upstream:
        monkeypatch.setattr("app.http_client.get_http_client", lambda: upstream)
        if readiness:
            response = await client.get("/readyz")
        else:
            response = await client.post(
                "/v1/chat/completions", headers=AUTH, json=BODY
            )
    assert response.status_code == 503 and len(calls) == 1
    if not readiness:
        await assert_no_account_penalty(accounts, 503)
    async with AsyncSessionLocal() as db:
        for i, cid in enumerate(accounts):
            credential = await db.get(UpstreamCredential, cid)
            assert credential.refresh_token == f"refresh-{i}"
            assert credential.failure_count == 0 and credential.cooldown_until is None


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, partial):
        self.partial = partial
        self.closed = False

    async def __aiter__(self):
        if self.partial:
            yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        raise httpx.ReadError("private proxy detail")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("partial", [False, True])
async def test_proxy_stream_failure_releases_slot_without_account_penalty(
    client, key, accounts, monkeypatch, stream, partial
):
    monkeypatch.setattr(settings, "upstream_proxy_url", "http://warp:1080")
    body = BrokenStream(partial)
    calls = []

    def fail(request):
        calls.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as upstream:
        monkeypatch.setattr("app.services.get_http_client", lambda: upstream)
        response = await client.post(
            "/v1/chat/completions", headers=AUTH, json={**BODY, "stream": stream}
        )
    assert response.status_code == (200 if stream and partial else 503)
    assert "upstream proxy" in response.text and "private" not in response.text
    assert len(calls) == 1 and body.closed
    await assert_no_account_penalty(accounts, 503)


@pytest.mark.parametrize("status", [401, 429])
async def test_real_account_http_errors_still_cool_and_fail_over(
    client, key, accounts, monkeypatch, status
):
    monkeypatch.setattr(settings, "upstream_proxy_url", "http://warp:1080")
    calls = []

    def handle(request):
        calls.append(request.headers["chatgpt-account-id"])
        if len(calls) == 1:
            return httpx.Response(
                status,
                json={"error": {"message": "account rejected"}},
                headers={"retry-after": "120"},
            )
        return httpx.Response(200, json={"output": [], "output_text": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr("app.services.get_http_client", lambda: upstream)
        response = await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    assert response.status_code == 200 and calls == [
        "proxy-account-0",
        "proxy-account-1",
    ]
    async with AsyncSessionLocal() as db:
        assert (await db.get(UpstreamCredential, accounts[0])).cooldown_until > utcnow()
        assert (await db.get(UpstreamCredential, accounts[1])).cooldown_until is None


@pytest.fixture
async def http_proxy(monkeypatch):
    await close_http_client()
    state = {"requests": []}
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            lines = headers.decode().split("\r\n")
            length = next(
                (
                    int(line.split(":", 1)[1])
                    for line in lines
                    if line.lower().startswith("content-length:")
                ),
                0,
            )
            body = await reader.readexactly(length) if length else b""
            state["requests"].append((lines[0], body))
            if lines[0].startswith("CONNECT "):
                writer.write(
                    b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                payload = (
                    {
                        "access_token": "refreshed",
                        "refresh_token": "rotated",
                        "expires_in": 3600,
                    }
                    if "/token " in lines[0]
                    else {"output": [], "output_text": "via proxy"}
                )
                data = json.dumps(payload).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                    + str(len(data)).encode()
                    + b"\r\n\r\n"
                    + data
                )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(settings, "upstream_proxy_url", f"http://127.0.0.1:{port}")
    # Explicit configuration must not silently bypass the proxy for NO_PROXY.
    monkeypatch.setenv("NO_PROXY", "*")
    try:
        yield state
    finally:
        await close_http_client()
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_real_http_proxy_routes_models_and_oauth_refresh(
    client, key, accounts, http_proxy, monkeypatch
):
    monkeypatch.setattr(settings, "upstream_base_url", "http://upstream.invalid")
    monkeypatch.setattr(settings, "oauth_token_url", "http://oauth.invalid/token")
    async with AsyncSessionLocal() as db:
        credential = await db.get(UpstreamCredential, accounts[0])
        credential.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    assert (
        response.status_code == 200
        and response.json()["choices"][0]["message"]["content"] == "via proxy"
    )
    await post_oauth({"grant_type": "authorization_code", "code": "mock-code"})
    lines = [line for line, _ in http_proxy["requests"]]
    assert lines == [
        "POST http://oauth.invalid/token HTTP/1.1",
        "POST http://upstream.invalid/codex/responses HTTP/1.1",
        "POST http://oauth.invalid/token HTTP/1.1",
    ]
    assert b"grant_type=refresh_token" in http_proxy["requests"][0][1]
    async with AsyncSessionLocal() as db:
        assert (
            await db.get(UpstreamCredential, accounts[0])
        ).refresh_token == "rotated"


async def test_connect_proxy_failure_does_not_fall_back_to_origin(
    client, key, accounts, http_proxy, monkeypatch
):
    monkeypatch.setattr(settings, "upstream_base_url", "https://upstream.invalid")
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BODY)
    assert response.status_code == 503
    assert [line for line, _ in http_proxy["requests"]] == [
        "CONNECT upstream.invalid:443 HTTP/1.1"
    ]
    await assert_no_account_penalty(accounts, 503)


@pytest.mark.parametrize(
    "trace,exit_code,healthy",
    [
        ("warp=on", 0, True),
        ("warp=plus", 0, True),
        ("warp=off", 0, False),
        ("notwarp=on", 0, False),
        ("warp=online", 0, False),
        ("warp=on", 7, False),
        ("", 0, False),
    ],
)
def test_warp_healthcheck_requires_successful_proxy_trace(
    tmp_path, trace, exit_code, healthy
):
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_TRACE"\nexit "$TEST_CURL_EXIT"\n')
    curl.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts" / "warp-healthcheck.sh"
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "TEST_TRACE": trace,
        "TEST_CURL_EXIT": str(exit_code),
    }
    result = subprocess.run(
        ["sh", str(script)], env=env, capture_output=True, timeout=3
    )
    assert (result.returncode == 0) is healthy
