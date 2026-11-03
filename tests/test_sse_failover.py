import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import (
    ApiKey,
    UpstreamAttempt,
    UpstreamCredential,
    UpstreamRequest,
    UsageLog,
    User,
    UserUpstreamRoute,
)

AUTH = {"Authorization": "Bearer gtw_test"}
OVERLOADED = "Our servers are currently overloaded. Please try again later."


def event(kind, **data):
    return {"type": kind, **data}


def failed(**response):
    return event(
        "response.failed",
        response={
            "status": "failed",
            "error": {
                "message": OVERLOADED,
                "type": "server_error",
                "code": "overloaded_error",
            },
            **response,
        },
    )


def completed():
    return event(
        "response.completed",
        response={
            "id": "new-response",
            "status": "completed",
            "output": [],
            "output_text": "ok",
            "usage": {"input_tokens": 4, "output_tokens": 2},
        },
    )


def life(identifier="new-response"):
    return event(
        "response.created",
        response={"id": identifier, "status": "in_progress", "output": []},
    )


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            if isinstance(chunk, float):
                await asyncio.sleep(chunk)
            elif isinstance(chunk, asyncio.Event):
                await chunk.wait()
            else:
                yield (
                    ("data: " + json.dumps(chunk) + "\n\n").encode()
                    if isinstance(chunk, dict)
                    else chunk
                )

    async def aclose(self):
        self.closed = True


@pytest.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(settings, "upstream_auth_mode", "oauth_manual")
    monkeypatch.setattr(settings, "sse_preview_max_seconds", 0.02)
    async with AsyncSessionLocal() as db:
        accounts = [
            UpstreamCredential(
                provider="codex", account_id=f"account-{i}", access_token=f"private-{i}"
            )
            for i in range(3)
        ]
        db.add_all(accounts)
        await db.commit()
        ids = [a.id for a in accounts]
    state = {"calls": [], "streams": [], "plans": {}, "active": []}

    async def handle(request):
        index = int(request.headers["chatgpt-account-id"].split("-")[-1])
        state["calls"].append(index)
        async with AsyncSessionLocal() as db:
            state["active"].append(
                await db.scalar(
                    select(func.count(UpstreamRequest.id)).where(
                        UpstreamRequest.active.is_(True)
                    )
                )
            )
        outcome = state["plans"].get(index, [life(), completed()])
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return httpx.Response(
                outcome,
                headers={"x-request-id": f"http-{index}"},
                json={"error": {"message": OVERLOADED, "code": "overloaded_error"}},
            )
        stream = Stream(outcome)
        state["streams"].append(stream)
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": f"request-{index}",
            },
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr("app.services.get_http_client", lambda: upstream)
        yield ids, state


async def request(client, path="/v1/responses", stream=True):
    payload = {"model": "gpt-test", "stream": stream}
    payload.update(
        {"messages": [{"role": "user", "content": "hello"}]}
        if path.endswith("completions")
        else {"input": "hello"}
    )
    return await client.post(path, headers=AUTH, json=payload)


async def rows():
    async with AsyncSessionLocal() as db:
        attempts = (
            await db.scalars(select(UpstreamAttempt).order_by(UpstreamAttempt.id))
        ).all()
        logs = (await db.scalars(select(UsageLog))).all()
        active = await db.scalar(
            select(func.count(UpstreamRequest.id)).where(
                UpstreamRequest.active.is_(True)
            )
        )
        return attempts, logs, active


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("lifecycle", [False, True])
async def test_early_failure_switches_without_leaking_and_single_usage(
    client, key, pool, path, stream, lifecycle
):
    ids, state = pool
    state["plans"][0] = (
        [
            life("old-response"),
            event(
                "response.in_progress",
                response={"id": "old-response", "status": "in_progress"},
            ),
        ]
        if lifecycle
        else []
    ) + [failed()]
    result = await request(client, path, stream)
    assert result.status_code == 200, result.text
    assert "old-response" not in result.text and OVERLOADED not in result.text
    assert state["calls"] == [0, 1]
    assert state["active"] == [1, 1]
    attempts, logs, active = await rows()
    assert [a.failure_category for a in attempts] == ["sse_business", None]
    assert [a.retry_decision for a in attempts] == ["retry_next_account", "return"]
    assert all(
        a.gateway_request_id == result.headers["x-gateway-request-id"] for a in attempts
    )
    assert (
        len(logs) == 1
        and logs[0].total_tokens == 6
        and logs[0].upstream_credential_id == ids[1]
    )
    assert logs[0].gateway_request_id == attempts[0].gateway_request_id
    assert active == 0 and all(s.closed for s in state["streams"])


async def test_successful_buffer_preserves_events_once_in_order(client, key, pool):
    _, state = pool
    source = [
        life(),
        event(
            "response.in_progress",
            response={"id": "new-response", "status": "in_progress"},
        ),
        event("response.output_text.delta", delta="ok"),
        completed(),
    ]
    state["plans"][0] = source
    result = await request(client)
    received = [
        json.loads(line[6:])
        for line in result.text.splitlines()
        if line.startswith("data: ")
    ]
    assert received == source
    assert state["calls"] == [0]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_preview_replay_preserves_split_utf8_and_sse_line_endings(
    client, key, pool, newline
):
    _, state = pool
    source = [
        life("流响应"),
        event("response.output_text.delta", delta="你好"),
        completed(),
    ]
    wire = "".join(
        "data: " + json.dumps(item, ensure_ascii=False) + newline * 2 for item in source
    ).encode()
    state["plans"][0] = [wire[i : i + 1] for i in range(len(wire))]
    result = await request(client)
    received = [
        json.loads(line[6:])
        for line in result.text.splitlines()
        if line.startswith("data: ")
    ]
    assert received == source and state["calls"] == [0]
    assert (await rows())[2] == 0


@pytest.mark.parametrize(
    "meaningful",
    [
        event("response.output_text.delta", delta="partial"),
        event("response.refusal.delta", delta="refusal"),
        event(
            "response.output_item.added",
            output_index=0,
            item={
                "type": "function_call",
                "name": "tool",
                "call_id": "call-1",
                "arguments": "",
            },
        ),
        event("response.reasoning_summary_text.delta", delta="reasoning"),
    ],
)
async def test_meaningful_output_never_replays(client, key, pool, meaningful):
    _, state = pool
    state["plans"][0] = [life(), meaningful, failed()]
    result = await request(client)
    assert result.status_code == 200 and OVERLOADED in result.text
    assert state["calls"] == [0]
    attempts, logs, active = await rows()
    assert (
        logs[0].status_code == 502
        and attempts[0].failure_category == "sse_business"
        and active == 0
    )


async def test_failure_with_embedded_output_never_replays(client, key, pool):
    _, state = pool
    state["plans"][0] = [failed(output_text="already computed")]
    response = await request(client)
    assert response.status_code == 502 and state["calls"] == [0]


@pytest.mark.parametrize("boundary", ["events", "bytes", "time"])
async def test_after_preview_boundary_no_replay(
    client, key, pool, monkeypatch, boundary
):
    _, state = pool
    if boundary == "events":
        monkeypatch.setattr(settings, "sse_preview_max_events", 1)
        source = [life(), failed()]
    elif boundary == "bytes":
        monkeypatch.setattr(settings, "sse_preview_max_bytes", 1024)
        source = [
            event(
                "response.created",
                response={"id": "old", "status": "in_progress", "padding": "x" * 2048},
            ),
            failed(),
        ]
    else:
        monkeypatch.setattr(settings, "sse_preview_max_seconds", 0.005)
        source = [life(), 0.03, failed()]
    state["plans"][0] = source
    result = await request(client)
    assert result.status_code == 200 and OVERLOADED in result.text
    assert state["calls"] == [0]
    assert (await rows())[2] == 0


async def test_mixed_http_and_sse_share_total_budget_and_keep_last_error(
    client, key, pool, monkeypatch
):
    _, state = pool
    monkeypatch.setattr(settings, "upstream_max_attempts", 2)
    state["plans"].update({0: 503, 1: [failed()], 2: [completed()]})
    response = await request(client, stream=False)
    assert response.status_code == 502 and OVERLOADED in response.text
    assert state["calls"] == [0, 1]
    attempts, logs, active = await rows()
    assert [a.failure_category for a in attempts] == ["upstream_http", "sse_business"]
    assert len(logs) == 1 and active == 0
    assert attempts[-1].upstream_request_id == "request-1"


async def test_no_backup_preserves_original_business_error(client, key, pool):
    ids, state = pool
    state["plans"][0] = [failed()]
    async with AsyncSessionLocal() as db:
        user = await db.get(User, key.user_id)
        user.routing_configured = True
        db.add(UserUpstreamRoute(user_id=user.id, upstream_credential_id=ids[0]))
        await db.commit()
    result = await request(client, stream=False)
    assert result.status_code == 502 and OVERLOADED in result.text
    assert result.headers["x-request-id"] == "request-0"
    assert state["calls"] == [0] and (await rows())[2] == 0


async def test_fixed_binding_never_switches(client, key, pool):
    ids, state = pool
    async with AsyncSessionLocal() as db:
        bound = await db.get(ApiKey, key.id)
        bound.upstream_credential_id = ids[0]
        await db.commit()
    state["plans"][0] = [failed()]
    assert (await request(client)).status_code == 502
    assert state["calls"] == [0]


async def test_user_routes_constrain_failover(client, key, pool):
    ids, state = pool
    async with AsyncSessionLocal() as db:
        user = await db.get(User, key.user_id)
        user.routing_configured = True
        db.add_all(
            UserUpstreamRoute(user_id=user.id, upstream_credential_id=cid)
            for cid in (ids[0], ids[2])
        )
        await db.commit()
    state["plans"][0] = [failed()]
    assert (await request(client)).status_code == 200
    assert state["calls"] == [0, 2]


@pytest.mark.parametrize(
    "failure",
    [httpx.ProxyError("secret-transport"), httpx.ReadError("secret-transport")],
)
async def test_proxy_failures_are_not_account_faults(
    client, key, pool, monkeypatch, failure, capsys
):
    ids, state = pool
    monkeypatch.setattr(settings, "upstream_proxy_url", "http://warp:1080")
    state["plans"][0] = [failure]
    response = await request(client)
    assert response.status_code == 503
    assert state["calls"] == [0]
    attempts, _, active = await rows()
    assert attempts[0].failure_category == "proxy_transport"
    assert attempts[0].transport_exception_type == type(failure).__name__
    async with AsyncSessionLocal() as db:
        assert (await db.get(UpstreamCredential, ids[0])).failure_count == 0
    assert active == 0 and "secret-transport" not in capsys.readouterr().out


async def test_truncated_stream_classified_separately(client, key, pool):
    _, state = pool
    state["plans"][0] = [life()]
    response = await request(client)
    assert "terminal" in response.text
    assert (await rows())[0][0].failure_category == "missing_terminal"


async def test_unexpected_stream_exception_is_500_not_499(client, key, pool, caplog):
    _, state = pool
    state["plans"][0] = [
        life(),
        event("response.output_text.delta", delta="partial"),
        RuntimeError("private-conversation"),
    ]
    # ASGITransport deliberately suppresses the propagated server exception.
    response = await request(client)
    assert response.status_code == 200
    attempts, logs, active = await rows()
    assert len(logs) == 1 and logs[0].status_code == 500
    assert logs[0].error_message == "gateway internal error"
    assert attempts[0].failure_category == "gateway_internal" and active == 0
    assert "private-conversation" not in caplog.text
    assert attempts[0].gateway_request_id in caplog.text


async def test_cancel_during_preview_releases_response_and_records_499(
    client, key, pool
):
    _, state = pool
    never = asyncio.Event()
    state["plans"][0] = [life(), never]
    task = asyncio.create_task(request(client))
    for _ in range(100):
        if state["streams"]:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    attempts, logs, active = await rows()
    assert active == 0 and all(s.closed for s in state["streams"])
    assert len(logs) == 1 and logs[0].status_code == 499
    assert attempts[0].failure_category == "client_cancelled"


async def test_disable_switch_rolls_back_retry_behavior(client, key, pool, monkeypatch):
    _, state = pool
    monkeypatch.setattr(settings, "sse_early_retry", False)
    state["plans"][0] = [failed()]
    assert (await request(client)).status_code == 502
    assert state["calls"] == [0]


async def test_preview_deadline_commits_even_without_a_complete_event(
    client, key, pool, monkeypatch
):
    _, state = pool
    monkeypatch.setattr(settings, "sse_preview_max_seconds", 0.005)
    state["plans"][0] = [0.04, failed()]
    result = await request(client)
    assert result.status_code == 200 and OVERLOADED in result.text
    assert state["calls"] == [0]


async def test_all_attempts_overloaded_keep_error_and_do_not_count_usage_twice(
    client, key, pool
):
    _, state = pool
    state["plans"].update({i: [failed()] for i in range(3)})
    response = await request(client, stream=False)
    assert response.status_code == 502 and OVERLOADED in response.text
    assert state["calls"] == [0, 1, 2]
    attempts, logs, active = await rows()
    assert len(attempts) == 3 and len(logs) == 1 and active == 0
    assert logs[0].upstream_request_id == "request-2"


async def test_streaming_failure_keeps_last_upstream_header_request_id(
    client, key, pool
):
    _, state = pool
    state["plans"].update({i: [failed()] for i in range(3)})
    response = await request(client)
    assert response.status_code == 502 and OVERLOADED in response.text
    assert response.headers["x-request-id"] == "request-2"


async def test_unknown_business_failure_does_not_replay(client, key, pool):
    _, state = pool
    state["plans"][0] = [
        event(
            "response.failed",
            response={
                "status": "failed",
                "error": {"message": "unknown failure", "code": "not_whitelisted"},
            },
        )
    ]
    response = await request(client)
    assert response.status_code == 502 and state["calls"] == [0]


async def test_connect_http_and_sse_failures_share_three_attempts(client, key, pool):
    _, state = pool
    state["plans"].update({0: httpx.ConnectError("private-url"), 1: 503, 2: [failed()]})
    result = await request(client, stream=False)
    assert result.status_code == 502 and OVERLOADED in result.text
    attempts, _, active = await rows()
    assert state["calls"] == [0, 1, 2] and active == 0
    assert [a.failure_category for a in attempts] == [
        "network_transport",
        "upstream_http",
        "sse_business",
    ]


async def test_original_body_request_id_survives_when_no_backup(client, key, pool):
    ids, state = pool
    async with AsyncSessionLocal() as db:
        for cid in ids[1:]:
            account = await db.get(UpstreamCredential, cid)
            account.enabled = False
        await db.commit()
    rid = "12345678-1234-1234-1234-123456789abc"
    state["plans"][0] = [failed(request_id=rid)]
    # Keep the transport's header id; the original body id remains in diagnostics.
    response = await request(client, stream=False)
    assert (
        response.status_code == 502 and response.json()["upstream"]["request_id"] == rid
    )
    assert response.headers["x-upstream-request-id"] == rid
    assert state["calls"] == [0]


@pytest.mark.parametrize("path", ["/v1/embeddings", "/v1/images/generations"])
async def test_raw_and_image_paths_do_not_gain_sse_replay(client, key, pool, path):
    _, state = pool
    state["plans"][0] = [failed()]
    response = await client.post(
        path,
        headers=AUTH,
        json={"model": "gpt-test", "input": "hello", "prompt": "image"},
    )
    assert response.status_code in (200, 502), response.text
    assert state["calls"] == [0]
    assert (await rows())[2] == 0


async def test_cancel_after_preview_deadline_closes_retained_pending_read(
    client, key, pool, monkeypatch
):
    _, state = pool
    monkeypatch.setattr(settings, "sse_preview_max_seconds", 0.005)
    state["plans"][0] = [life(), asyncio.Event()]
    task = asyncio.create_task(request(client))
    await asyncio.sleep(0.04)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    attempts, logs, active = await rows()
    assert active == 0 and all(stream.closed for stream in state["streams"])
    assert len(logs) == 1 and logs[0].status_code == 499
    assert attempts[0].failure_category == "client_cancelled"


async def test_local_capacity_is_distinct_from_upstream_failure(
    client, key, pool, monkeypatch
):
    from app.upstreams import claim_account

    ids, state = pool
    async with AsyncSessionLocal() as db:
        for cid in ids:
            account = await db.get(UpstreamCredential, cid)
            account.max_concurrency = 1
        await db.commit()
    monkeypatch.setattr(settings, "upstream_capacity_wait_seconds", 0.01)
    leases = []
    try:
        for cid in ids:
            leases.append(await claim_account(cid, set()))
        response = await request(client)
        assert response.status_code == 429
        attempts, logs, _ = await rows()
        assert attempts[0].failure_category == "local_capacity"
        assert state["calls"] == [] and len(logs) == 1
    finally:
        for lease in leases:
            await lease.release()


async def test_preview_internal_exception_is_classified_and_sanitized(
    client, key, pool, caplog
):
    _, state = pool
    state["plans"][0] = [RuntimeError("private-conversation")]
    response = await request(client)
    attempts, logs, active = await rows()
    assert response.status_code == 500 and "private-conversation" not in response.text
    assert response.headers["x-gateway-request-id"] == attempts[0].gateway_request_id
    assert (
        logs[0].status_code == 500
        and attempts[0].failure_category == "gateway_internal"
    )
    assert active == 0 and all(s.closed for s in state["streams"])
    assert "private-conversation" not in caplog.text


async def test_uncaught_server_exception_message_is_sanitized(key, pool):
    from app.attempts import GatewayInternalError
    from app.main import app

    _, state = pool
    state["plans"][0] = [RuntimeError("private-conversation")]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as strict_client:
        with pytest.raises(GatewayInternalError) as caught:
            await request(strict_client)
    assert "private-conversation" not in str(caught.value)
    assert "gw_" in str(caught.value)


@pytest.mark.parametrize("http_status", [200, 503])
async def test_overload_code_has_consistent_http_and_sse_cooldown_policy(
    client, key, pool, monkeypatch, http_status
):
    ids, _ = pool
    monkeypatch.setattr(settings, "upstream_max_attempts", 1)
    error = {"message": "busy", "code": "overloaded_error"}

    async def send(*args):
        return httpx.Response(
            http_status,
            **(
                {"json": {"error": error}}
                if http_status != 200
                else {
                    "headers": {"content-type": "text/event-stream"},
                    "stream": Stream([failed(error=error)]),
                }
            ),
        )

    monkeypatch.setattr("app.services._send_upstream", send)
    response = await request(client)
    assert response.status_code == (503 if http_status == 503 else 502)
    async with AsyncSessionLocal() as db:
        assert (await db.get(UpstreamCredential, ids[0])).failure_count == 0


@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
async def test_downstream_send_disconnect_closes_suspended_stream(key, pool, asgi_spec):
    from starlette.requests import ClientDisconnect

    from app.main import app

    _, state = pool
    # Stop preview on the first text event, before the body generator has ended.
    state["plans"][0] = [
        life(),
        event("response.output_text.delta", delta="partial"),
        completed(),
    ]
    payload = json.dumps({"model": "gpt-test", "input": "hello", "stream": True})
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": payload.encode()}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("private-connection-detail")

    with pytest.raises(ClientDisconnect):
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": asgi_spec},
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
    attempts, logs, active = await rows()
    assert len(logs) == 1 and logs[0].status_code == 499
    assert attempts[0].failure_category == "client_cancelled"
    assert active == 0 and all(stream.closed for stream in state["streams"])
