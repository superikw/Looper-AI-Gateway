import asyncio
import json
import re

import httpx
import pytest
from test_admin_app import admin_client as _admin_client
from test_sse_failover import AUTH, Stream, completed, failed, life, request, rows
from test_sse_failover import pool as _pool

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import ApiKey, UpstreamCredential

pool = _pool
admin_client = _admin_client


@pytest.fixture(autouse=True)
def native_defaults(monkeypatch, legacy_adapter_defaults):
    monkeypatch.setattr(settings, "responses_passthrough", True)


@pytest.mark.parametrize("content_type", [None, "text/event-stream"])
async def test_native_body_headers_events_and_trailing_frames_are_preserved(
    client, key, pool, content_type
):
    _, state = pool
    source = (
        ": upstream heartbeat\r\n\r\nid: one\r\nretry: 900\r\nevent: vendor.event\r\n"
        'data: {"type":"vendor.event","payload":"你好"}\r\n\r\n'
        + "data: "
        + json.dumps(completed())
        + "\r\n\r\n"
        + ': after completion\n\nevent: vendor.tail\ndata: {"type":"vendor.tail"}\n\ndata: [DONE]\n\n'
    ).encode()
    stream = Stream([source[i : i + 1] for i in range(len(source))])
    headers = {"x-provider-trace": "trace", "set-cookie": "private-token=secret"}
    if content_type:
        headers["content-type"] = content_type
    state["plans"][0] = httpx.Response(200, headers=headers, stream=stream)
    body = b'{ "model":"GPT_Test", "input":"hello", "stream":true, "store":true, "future_option":{"x":1} }'
    response = await client.post(
        "/v1/responses?future_query=yes",
        content=body,
        headers={
            **AUTH,
            "content-type": "application/json",
            "user-agent": "Codex-test",
            "x-client-session": "session",
            "cookie": "private",
            "chatgpt-account-id": "wrong",
        },
    )
    assert response.content == source
    assert response.headers["x-provider-trace"] == "trace"
    assert "set-cookie" not in response.headers
    assert response.headers.get("content-type") == content_type
    upstream = state["requests"][0]
    assert upstream.content == body and upstream.url.query == b"future_query=yes"
    assert upstream.headers["user-agent"] == "Codex-test"
    assert upstream.headers["x-client-session"] == "session"
    assert upstream.headers["authorization"] == "Bearer private-0"
    assert upstream.headers["chatgpt-account-id"] == "account-0"
    assert "cookie" not in upstream.headers
    assert stream.closed and (await rows())[2] == 0


async def test_native_failover_logs_each_attempt_and_counts_user_once(
    client, key, pool, admin_client
):
    ids, state = pool
    state["plans"][0] = [
        life("old"),
        failed(usage={"input_tokens": 2, "output_tokens": 1}),
    ]
    response = await request(client)
    assert response.status_code == 200 and "old" not in response.text
    attempts, logs, active = await rows()
    assert len(attempts) == len(logs) == 2 and active == 0
    assert [log.retry_count for log in logs] == [0, 1]
    assert [log.status_code for log in logs] == [502, 200]
    assert [log.upstream_http_status for log in logs] == [200, 200]
    assert [log.upstream_credential_id for log in logs] == ids[:2]
    assert [log.is_final for log in logs] == [False, True]
    assert [log.total_tokens for log in logs] == [3, 6]
    rid = response.headers["x-gateway-request-id"]
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{8}", rid)
    assert all(log.gateway_request_id == rid for log in logs)
    assert [log.upstream_request_id for log in logs] == ["request-0", "request-1"]
    assert attempts[0].retry_reason == "retryable_business_error"
    assert attempts[0].preview_stop_reason == "retryable_failure"
    assert attempts[0].failure_event_ms is not None
    assert attempts[0].response_headers_ms is not None
    assert attempts[0].preview_elapsed_ms is not None
    assert attempts[1].failure_event_ms is None
    async with AsyncSessionLocal() as db:
        saved = await db.get(ApiKey, key.id)
        assert saved.request_count == 1 and saved.total_tokens == 9
    await admin_client.post(
        "/api/login", json={"username": "root", "password": "root-password-123"}
    )
    api_logs = (await admin_client.get("/api/logs")).json()
    assert {log["display_request_id"] for log in api_logs} == {rid + "/0", rid + "/1"}
    assert (await admin_client.get("/api/dashboard")).json()["periods"]["day"][
        "requests"
    ] == 1
    chain = (await admin_client.get(f"/api/logs/{logs[0].id}/attempts")).json()
    assert chain[0]["retry_reason"] == "retryable_business_error"
    assert chain[0]["preview_stop_reason"] == "retryable_failure"
    assert chain[0]["response_headers_ms"] == attempts[0].response_headers_ms
    assert chain[0]["failure_event_ms"] == attempts[0].failure_event_ms


@pytest.mark.parametrize(
    "code", ["overloaded_error", "service_unavailable", "server_is_overloaded"]
)
async def test_native_early_errors_retry_without_changing_success_stream(
    client, key, pool, code
):
    _, state = pool
    state["omit_content_type"] = True
    state["plans"][0] = [
        failed(
            error={
                "type": "service_unavailable_error",
                "code": code,
                "message": "temporary failure",
            }
        )
    ]
    response = await request(client)
    assert response.status_code == 200 and state["calls"] == [0, 1]
    assert (
        response.content
        == (
            "data: "
            + json.dumps(life())
            + "\n\n"
            + "data: "
            + json.dumps(completed())
            + "\n\n"
        ).encode()
    )


async def test_native_error_after_output_is_forwarded_without_replay(client, key, pool):
    _, state = pool
    data = [
        life(),
        {"type": "response.output_text.delta", "delta": "partial"},
        failed(),
    ]
    state["plans"][0] = data
    response = await request(client)
    assert response.status_code == 200 and state["calls"] == [0]
    assert (
        response.content
        == "".join("data: " + json.dumps(item) + "\n\n" for item in data).encode()
    )
    assert (await rows())[1][0].status_code == 502
    diagnostic = (await rows())[0][0]
    assert diagnostic.retry_reason == "meaningful_output"
    assert diagnostic.preview_stop_reason == "meaningful_output"
    assert diagnostic.failure_event_ms is not None


async def test_native_no_spare_keeps_original_failed_event_and_single_log(
    client, key, pool
):
    ids, state = pool
    state["plans"][0] = [failed()]
    async with AsyncSessionLocal() as db:
        for cid in ids[1:]:
            (await db.get(UpstreamCredential, cid)).enabled = False
        await db.commit()
    response = await request(client)
    assert response.status_code == 200
    assert response.content == ("data: " + json.dumps(failed()) + "\n\n").encode()
    _, logs, active = await rows()
    assert len(logs) == 1 and logs[0].is_final and active == 0
    diagnostic = (await rows())[0][0]
    assert diagnostic.retry_reason == "no_available_account"
    async with AsyncSessionLocal() as db:
        assert (await db.get(ApiKey, key.id)).request_count == 1


@pytest.mark.parametrize(
    "payload", [{"previous_response_id": "resp_old"}, {"conversation": "conv_old"}]
)
async def test_stateful_request_does_not_fail_over(client, key, pool, payload):
    _, state = pool
    state["plans"][0] = [failed()]
    await client.post(
        "/v1/responses",
        headers=AUTH,
        json={"model": "gpt-test", "input": [], "stream": True, **payload},
    )
    assert state["calls"] == [0]
    diagnostic = (await rows())[0][0]
    assert diagnostic.retry_reason == "stateful_request"
    assert diagnostic.preview_stop_reason == "retryable_failure"


@pytest.mark.parametrize(
    "status,error,headers",
    [
        (429, {"code": "insufficient_quota"}, {}),
        (503, {"code": "service_unavailable"}, {"retry-after": "30"}),
    ],
)
async def test_quota_or_excessive_retry_after_stops_and_preserves_error(
    client, key, pool, status, error, headers
):
    _, state = pool
    wire = json.dumps({"error": error}, indent=2).encode()
    state["plans"][0] = httpx.Response(
        status, headers={"content-type": "application/json", **headers}, content=wire
    )
    response = await request(client)
    assert response.status_code == status and response.content == wire
    assert state["calls"] == [0] and len((await rows())[1]) == 1
    diagnostic = (await rows())[0][0]
    assert diagnostic.retry_reason == (
        "permanent_error" if status == 429 else "retry_after_exceeds_limit"
    )


async def test_backoff_is_bounded_and_releases_lease_before_waiting(
    client, key, pool, monkeypatch
):
    _, state = pool
    state["plans"].update({0: [failed()], 1: [failed()]})
    monkeypatch.setattr(settings, "upstream_retry_base_delay_seconds", 0.25)
    monkeypatch.setattr("app.retry_policy.random.random", lambda: 0)
    waits = []

    async def sleep(delay):
        waits.append(delay)
        assert (await rows())[2] == 0
        assert all(stream.closed for stream in state["streams"])

    monkeypatch.setattr("app.services.sleep_retry", sleep)
    response = await request(client)
    assert response.status_code == 200 and waits == [0.25, 0.5]
    assert [row.retry_count for row in (await rows())[1]] == [0, 1, 2]


async def test_backoff_budget_exhaustion_does_not_make_another_attempt(
    client, key, pool, monkeypatch
):
    _, state = pool
    state["plans"][0] = [failed()]
    monkeypatch.setattr(settings, "upstream_retry_base_delay_seconds", 1)
    monkeypatch.setattr(settings, "upstream_retry_max_wait_seconds", 0.1)
    await request(client)
    assert state["calls"] == [0]
    assert (await rows())[0][0].retry_reason == "backoff_budget_exhausted"


async def test_native_cancellation_releases_all_resources(client, key, pool):
    _, state = pool
    state["plans"][0] = [life(), asyncio.Event()]
    task = asyncio.create_task(request(client))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _, logs, active = await rows()
    assert active == 0 and all(stream.closed for stream in state["streams"])
    assert len(logs) == 1 and logs[0].status_code == 499


async def test_native_json_body_is_unchanged(client, key, pool):
    _, state = pool
    wire = b' { "output_text":"ok", "usage":{"input_tokens":3}, "future":{"x":true} } '
    state["plans"][0] = httpx.Response(200, content=wire)
    response = await request(client, stream=False)
    assert response.content == wire
    assert json.loads(state["requests"][0].content)["stream"] is False
    assert (await rows())[1][0].total_tokens == 3


@pytest.mark.parametrize("boundary", ["bytes", "time"])
async def test_http_error_inspection_is_bounded_and_lossless(
    client, key, pool, monkeypatch, boundary
):
    _, state = pool
    body = json.dumps({"error": {"message": "x" * 3000}}).encode()
    if boundary == "bytes":
        monkeypatch.setattr(settings, "sse_preview_max_bytes", 1024)
        chunks = [body]
    else:
        chunks = [body[:1], 0.04, body[1:]]
    source = Stream(chunks)
    state["plans"][0] = httpx.Response(
        503, headers={"content-type": "application/json"}, stream=source
    )
    response = await request(client)
    assert response.status_code == 503 and response.content == body
    assert state["calls"] == [0] and source.closed and (await rows())[2] == 0


async def test_compressed_native_response_is_not_double_decoded(client, key, pool):
    import gzip

    _, state = pool
    body = ("data: " + json.dumps(completed()) + "\n\n").encode()
    state["plans"][0] = httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
        stream=Stream([gzip.compress(body)]),
    )
    response = await request(client)
    assert response.content == body and "content-encoding" not in response.headers
    assert (await rows())[1][0].total_tokens == 6


async def test_cancel_after_completed_keeps_successful_upstream_accounting(
    client, key, pool
):
    _, state = pool
    state["plans"][0] = [completed(), asyncio.Event()]
    task = asyncio.create_task(request(client))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _, logs, active = await rows()
    assert len(logs) == 1 and logs[0].status_code == 200 and logs[0].total_tokens == 6
    assert active == 0 and all(stream.closed for stream in state["streams"])


async def test_cancel_during_backoff_keeps_one_failed_upstream_log(
    client, key, pool, monkeypatch
):
    _, state = pool
    state["plans"][0] = [failed()]
    monkeypatch.setattr(settings, "upstream_retry_base_delay_seconds", 0.25)
    entered = asyncio.Event()

    async def sleep(delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.services.sleep_retry", sleep)
    task = asyncio.create_task(request(client))
    await asyncio.wait_for(entered.wait(), 1)
    assert len((await rows())[1]) == 1 and (await rows())[2] == 0
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _, logs, active = await rows()
    assert len(logs) == 1 and logs[0].retry_count == 0 and logs[0].is_final
    assert state["calls"] == [0] and active == 0
    async with AsyncSessionLocal() as db:
        assert (await db.get(ApiKey, key.id)).request_count == 1


async def test_native_proxy_failure_is_not_account_penalty(
    client, key, pool, monkeypatch
):
    ids, state = pool
    monkeypatch.setattr(settings, "upstream_proxy_url", "http://warp:1080")
    state["plans"][0] = [
        life(),
        {"type": "response.output_text.delta", "delta": "partial"},
        httpx.ReadError("private-transport"),
    ]
    await request(client)
    attempts, logs, active = await rows()
    assert state["calls"] == [0] and len(logs) == 1 and active == 0
    assert (
        attempts[0].failure_category == "proxy_transport" and logs[0].status_code == 503
    )
    async with AsyncSessionLocal() as db:
        assert (await db.get(UpstreamCredential, ids[0])).failure_count == 0


async def test_native_is_incremental_even_when_upstream_has_not_finished(key, pool):
    from app.main import app

    _, state = pool
    release = asyncio.Event()
    state["plans"][0] = [b": keepalive\n\n", release, completed()]
    emitted = asyncio.Event()
    received = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {
                "type": "http.request",
                "body": b'{"model":"gpt-test","input":[],"stream":true}',
            }
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            received.append(message["body"])
            emitted.set()

    task = asyncio.create_task(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/responses",
                "raw_path": b"/v1/responses",
                "query_string": b"",
                "headers": [
                    (b"authorization", b"Bearer gtw_test"),
                    (b"content-type", b"application/json"),
                ],
                "server": ("test", 80),
                "client": ("127.0.0.1", 1234),
            },
            receive,
            send,
        )
    )
    try:
        await asyncio.wait_for(emitted.wait(), 1)
        assert b"".join(received) == b": keepalive\n\n" and not task.done()
        release.set()
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (await rows())[1][0].total_tokens == 6


@pytest.mark.parametrize("content_type", [None, "application/json"])
async def test_known_json_business_failure_retries_without_sse_conversion(
    client, key, pool, content_type
):
    _, state = pool
    body = b' { "error": { "type":"service_unavailable", "message":"temporary" } } '
    state["plans"][0] = httpx.Response(
        200,
        headers={"content-type": content_type} if content_type else {},
        content=body,
    )
    response = await request(client)
    attempts, logs, active = await rows()
    assert response.status_code == 200 and state["calls"] == [0, 1] and active == 0
    assert attempts[0].failure_category == "upstream_business"
    assert logs[0].status_code == 502 and [row.retry_count for row in logs] == [0, 1]


async def test_native_observation_does_not_reject_unknown_or_malformed_metadata(
    client, key, pool
):
    _, state = pool
    wire = b'data: {"type":"response.created","response":{"output":[{"type":"function_call","id":"new-schema"}]}}\n\ndata: not-json\n\ndata: {"type":"error","code":[],"message":"failure"}\n\n'
    state["plans"][0] = [wire]
    response = await request(client)
    assert response.content == wire and state["calls"] == [0]
    assert (await rows())[2] == 0


async def test_retry_after_is_a_minimum_wait(client, key, pool, monkeypatch):
    _, state = pool
    state["plans"][0] = httpx.Response(
        503,
        headers={"retry-after": "0.5"},
        json={"error": {"code": "service_unavailable"}},
    )
    monkeypatch.setattr("app.retry_policy.random.random", lambda: 0)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.services.sleep_retry", sleep)
    await request(client)
    assert delays == [0.5] and state["calls"] == [0, 1]


async def test_native_tool_loop_preserves_call_ids_and_replayed_input(
    client, key, pool
):
    _, state = pool
    call = {
        "type": "function_call",
        "id": "fc-original",
        "call_id": "call-original",
        "name": "list_files",
        "arguments": "{}",
    }
    first = [
        {"type": "response.output_item.added", "output_index": 0, "item": call},
        {
            "type": "response.completed",
            "response": {
                "id": "tool-response",
                "status": "completed",
                "output": [call],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        },
    ]
    state["plans"][0] = first
    response = await client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "gpt-test",
            "input": "list files",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "list_files",
                    "parameters": {"type": "object"},
                }
            ],
        },
    )
    assert (
        response.content
        == "".join("data: " + json.dumps(event) + "\n\n" for event in first).encode()
    )
    continuation = {
        "model": "gpt-test",
        "stream": True,
        "input": [
            {"role": "user", "content": "list files"},
            call,
            {
                "type": "function_call_output",
                "call_id": "call-original",
                "output": "README.md",
            },
        ],
        "reasoning": {"effort": "high"},
        "future_client_field": True,
    }
    wire = json.dumps(continuation, ensure_ascii=False, indent=2).encode()
    followup = await client.post(
        "/v1/responses",
        headers={**AUTH, "content-type": "application/json"},
        content=wire,
    )
    assert (
        followup.status_code == 200 and '"type": "response.completed"' in followup.text
    )
    assert state["requests"][1].content == wire
    assert (
        response.headers["x-gateway-request-id"]
        != followup.headers["x-gateway-request-id"]
    )
    async with AsyncSessionLocal() as db:
        assert (await db.get(ApiKey, key.id)).request_count == 2


async def test_utf8_bom_does_not_hide_early_failure(client, key, pool):
    _, state = pool
    state["omit_content_type"] = True
    wire = b"\xef\xbb\xbf" + ("data: " + json.dumps(failed()) + "\n\n").encode()
    state["plans"][0] = [wire[i : i + 1] for i in range(len(wire))]
    response = await request(client)
    assert response.status_code == 200 and state["calls"] == [0, 1]
    assert len((await rows())[1]) == 2


async def test_large_observation_does_not_truncate_or_invent_failure(client, key, pool):
    _, state = pool
    event = completed()
    event["response"]["output_text"] = "x" * (1024 * 1024 + 32768)
    wire = ("data: " + json.dumps(event) + "\n\n").encode()
    state["plans"][0] = [wire]
    response = await request(client)
    assert response.content == wire and response.status_code == 200
    _, logs, active = await rows()
    assert (
        logs[0].status_code == 200 and logs[0].failure_category is None and active == 0
    )


@pytest.mark.parametrize("stream", [True, False])
async def test_preview_timeout_records_failure_time_without_replay(
    client, key, pool, monkeypatch, stream
):
    _, state = pool
    monkeypatch.setattr(settings, "sse_preview_max_seconds", 0.01)
    data = [life(), 0.06, failed()]
    state["plans"][0] = data
    response = await request(client, stream=stream)
    assert state["calls"] == [0] and response.status_code == 200
    assert response.content == b"".join(
        ("data: " + json.dumps(event) + "\n\n").encode()
        for event in data
        if isinstance(event, dict)
    )
    attempts, logs, active = await rows()
    diagnostic = attempts[0]
    assert diagnostic.retry_reason == "preview_timeout"
    assert diagnostic.preview_stop_reason == "preview_timeout"
    assert 8 <= diagnostic.preview_elapsed_ms < diagnostic.failure_event_ms
    assert diagnostic.failure_event_ms >= 50
    assert diagnostic.response_headers_ms >= 0
    assert diagnostic.elapsed_ms >= diagnostic.response_headers_ms + 50
    assert len(attempts) == len(logs) == 1 and active == 0


@pytest.mark.parametrize(
    "boundary,expected",
    [
        ("events", "preview_event_limit"),
        ("bytes", "preview_byte_limit"),
        ("unknown", "unknown_event"),
        ("error", "non_retryable_error"),
        ("disabled", "early_retry_disabled"),
        ("attempts", "attempt_limit"),
        ("binding", "bound_account"),
    ],
)
async def test_no_retry_reason_is_persisted_without_changing_body(
    client, key, pool, monkeypatch, boundary, expected
):
    ids, state = pool
    source = [failed()]
    if boundary == "events":
        monkeypatch.setattr(settings, "sse_preview_max_events", 1)
        source.insert(0, life())
    elif boundary == "bytes":
        monkeypatch.setattr(settings, "sse_preview_max_bytes", 1024)
        source.insert(0, b":" + b"x" * 2048 + b"\n\n")
    elif boundary == "unknown":
        source.insert(0, {"type": "vendor.event", "private_content": "secret-text"})
    elif boundary == "error":
        source = [failed(error={"code": "unknown", "message": "different failure"})]
    elif boundary == "disabled":
        monkeypatch.setattr(settings, "sse_early_retry", False)
    elif boundary == "attempts":
        monkeypatch.setattr(settings, "upstream_max_attempts", 1)
    elif boundary == "binding":
        async with AsyncSessionLocal() as db:
            (await db.get(ApiKey, key.id)).upstream_credential_id = ids[0]
            await db.commit()
    state["plans"][0] = source
    response = await request(client)
    assert response.content == b"".join(
        item
        if isinstance(item, bytes)
        else ("data: " + json.dumps(item) + "\n\n").encode()
        for item in source
    )
    attempts, _, active = await rows()
    assert state["calls"] == [0] and active == 0
    assert attempts[0].retry_reason == expected
    assert attempts[0].failure_event_ms is not None
    assert attempts[0].response_headers_ms is not None
    assert "secret-text" not in json.dumps(attempts[0].__dict__, default=str)


async def test_failure_time_excludes_connection_wait(client, key, pool, monkeypatch):
    import app.services as services

    _, state = pool
    state["plans"][0] = [failed()]
    monkeypatch.setattr(settings, "upstream_max_attempts", 1)
    send = services._send_upstream

    async def delayed_send(*args, **kwargs):
        await asyncio.sleep(0.06)
        return await send(*args, **kwargs)

    monkeypatch.setattr(services, "_send_upstream", delayed_send)
    await request(client)
    diagnostic = (await rows())[0][0]
    assert diagnostic.response_headers_ms >= 50
    assert diagnostic.failure_event_ms < diagnostic.response_headers_ms


async def test_failure_observation_does_not_include_trailing_stream_wait(
    client, key, pool
):
    _, state = pool
    source = [
        {"type": "response.output_text.delta", "delta": "partial"},
        failed(),
        0.08,
        b": trailing heartbeat\n\n",
    ]
    state["plans"][0] = source
    await request(client)
    diagnostic = (await rows())[0][0]
    assert diagnostic.elapsed_ms - diagnostic.failure_event_ms >= 60
    assert diagnostic.retry_reason == "meaningful_output"
