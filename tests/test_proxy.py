import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.db import AsyncSessionLocal
from app.models import UpstreamCredential, UsageLog
from app.protocol import build_codex_request_body, extract_sse_json, parse_sse_events
from app.proxy_schemas import validate_proxy_payload
from app.services import read_upstream_data

AUTH = {"Authorization": "Bearer gtw_test"}
BASE = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]}


def events(*items, sep="\n"):
    return "".join(
        "data: " + json.dumps(item, ensure_ascii=False) + sep + sep for item in items
    )


@pytest.fixture
async def upstream(monkeypatch):
    state = {"requests": []}
    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamCredential(
                provider="codex", access_token="mock-token", account_id="mock-account"
            )
        )
        await db.commit()

    def handler(request):
        state["requests"].append(request)
        if "exception" in state:
            raise state["exception"]
        return httpx.Response(
            state.get("status", 200),
            headers={
                "content-type": state.get("content_type", "text/event-stream"),
                "x-request-id": "mock-request",
            },
            content=state.get(
                "body",
                events(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_test",
                            "status": "completed",
                            "output": [],
                            "output_text": "hello",
                            "usage": {
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "total_tokens": 5,
                            },
                        },
                    }
                ),
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.services.get_http_client", lambda: client)

    yield state
    await client.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"messages": []},
        {"model": 3, "messages": []},
        {**BASE, "stream": "true"},
        {
            **BASE,
            "messages": [{"role": "user", "content": [{"type": "audio", "data": "x"}]}],
        },
    ],
)
async def test_invalid_payloads_are_400_and_audited(client, key, payload):
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, content=json.dumps(payload)
    )
    assert response.status_code == 400, response.text
    assert "error" in response.json()
    async with AsyncSessionLocal() as db:
        assert (
            await db.scalar(
                select(func.count(UsageLog.id)).where(UsageLog.status_code == 400)
            )
            == 1
        )


async def test_failed_upstream_event_is_not_success(client, key, upstream):
    upstream["body"] = events(
        {
            "type": "response.failed",
            "response": {"status": "failed", "error": {"message": "mock failure"}},
        }
    )
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "mock failure"
    assert "choices" not in response.json()


async def test_truncated_stream_does_not_become_success(client, key, upstream):
    upstream["body"] = events(
        {"type": "response.output_text.delta", "delta": "partial"}
    )
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    assert response.status_code == 502
    assert "terminal" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "exc,status", [(httpx.ReadTimeout("mock"), 504), (httpx.ConnectError("mock"), 502)]
)
async def test_network_errors_are_audited(client, key, upstream, exc, status):
    upstream["exception"] = exc
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    assert response.status_code == status
    assert "error" in response.json()
    async with AsyncSessionLocal() as db:
        log = await db.scalar(select(UsageLog))
        assert log.status_code == status


async def test_no_prompt_or_token_is_written_to_logs(client, key, upstream, capsys):
    await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            **BASE,
            "messages": [{"role": "user", "content": "private-prompt-value"}],
        },
    )
    output = capsys.readouterr().out
    assert "private-prompt-value" not in output
    assert "mock-token" not in output
    assert "mock-account" not in output


def test_crlf_sse_and_failure_detection():
    text = events(
        {"type": "response.output_text.delta", "delta": "你好"},
        {"type": "response.completed", "response": {"status": "completed"}},
        sep="\r\n",
    )
    assert len(parse_sse_events(text)) == 2
    assert extract_sse_json(text)["output_text"] == "你好"


def test_images_tools_and_json_schema_are_converted():
    payload = {
        **BASE,
        "messages": [
            {"role": "system", "content": "follow rules"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,eA==",
                            "detail": "high",
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Paris"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "weather", "parameters": {"type": "object"}},
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "weather"}},
        "max_tokens": 100,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "schema": {"type": "object"},
                "strict": True,
            },
        },
    }
    body = build_codex_request_body(
        "/v1/chat/completions", validate_proxy_payload("/v1/chat/completions", payload)
    )
    assert body["instructions"] == "follow rules"
    assert body["input"][0]["content"][1]["type"] == "input_image"
    assert body["input"][1]["type"] == "function_call"
    assert body["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "sunny",
    }
    assert body["tools"][0]["name"] == "weather"
    assert body["tool_choice"] == {"type": "function", "name": "weather"}
    assert body["max_output_tokens"] == 100
    assert body["text"]["format"]["name"] == "answer"


def test_client_specific_fields_are_preserved_and_forwarded():
    payload = {
        "model": "gpt-6.1-sol",
        "input": "hello",
        "prompt_cache_key": "session-cache-key",
        "client_metadata": {"session_id": "session-1", "origin": "codex"},
        "future_option": {"enabled": True},
    }
    normalized = validate_proxy_payload("/v1/responses", payload)
    body = build_codex_request_body("/v1/responses", normalized)
    assert body["prompt_cache_key"] == "session-cache-key"
    assert body["client_metadata"] == payload["client_metadata"]
    assert body["future_option"] == payload["future_option"]


async def test_nonstream_tool_calls_and_length_reason(client, key, upstream):
    upstream["body"] = events(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_call",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "weather",
                        "arguments": "{}",
                    }
                ],
            },
        }
    )
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["id"] == "call_1"
    upstream["body"] = events(
        {
            "type": "response.incomplete",
            "response": {
                "status": "incomplete",
                "output": [],
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        }
    )
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    assert response.json()["choices"][0]["finish_reason"] == "length"


async def test_stream_true_returns_sse_and_final_usage(client, key, upstream):
    upstream["body"] = events(
        {"type": "response.created", "response": {"id": "resp_test"}},
        {"type": "response.output_text.delta", "delta": "hello"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        },
        sep="\r\n",
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={**BASE, "stream": True, "stream_options": {"include_usage": True}},
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-request-id"] == "mock-request"
    chunks = parse_sse_events(response.text)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[1]["choices"][0]["delta"]["content"] == "hello"
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 5
    assert response.text.endswith("data: [DONE]\n\n")
    async with AsyncSessionLocal() as db:
        logs = (await db.scalars(select(UsageLog))).all()
        assert len(logs) == 1
        assert (logs[0].status_code, logs[0].total_tokens) == (200, 5)


async def test_midstream_failure_emits_error_and_no_success_finish(
    client, key, upstream
):
    upstream["body"] = events(
        {"type": "response.output_text.delta", "delta": "partial"},
        {
            "type": "response.failed",
            "response": {"error": {"message": "failed"}, "status": "failed"},
        },
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**BASE, "stream": True}
    )
    chunks = parse_sse_events(response.text)
    assert chunks[-1]["error"]["message"] == "failed"
    assert not any(
        choice.get("finish_reason")
        for chunk in chunks
        for choice in chunk.get("choices", [])
    )
    async with AsyncSessionLocal() as db:
        assert (await db.scalar(select(UsageLog))).status_code == 502


async def test_stream_function_arguments(client, key, upstream):
    upstream["body"] = events(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "weather",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '{"city":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '"Paris"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "arguments": '{"city":"Paris"}',
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "weather",
                        "arguments": '{"city":"Paris"}',
                    }
                ],
            },
        },
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**BASE, "stream": True}
    )
    chunks = parse_sse_events(response.text)
    calls = [
        call
        for chunk in chunks
        for choice in chunk.get("choices", [])
        for call in choice["delta"].get("tool_calls", [])
    ]
    assert calls[0]["id"] == "call_1"
    assert (
        "".join(call["function"].get("arguments", "") for call in calls)
        == '{"city":"Paris"}'
    )
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_responses_stream_preserves_typed_events(client, key, upstream):
    response = await client.post(
        "/v1/responses",
        headers=AUTH,
        json={"model": "gpt-5.5", "input": "hello", "stream": True},
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: response.completed" in response.text
    assert parse_sse_events(response.text)[0]["response"]["id"] == "resp_test"


class DelayedStream(httpx.AsyncByteStream):
    def __init__(self):
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        # Split UTF-8 and CRLF across transport chunks as a real socket can.
        data = events(
            {"type": "response.output_text.delta", "delta": "你好"}, sep="\r\n"
        ).encode()
        split = data.index("你".encode()) + 1
        yield data[:split]
        yield data[split:]
        await self.release.wait()
        yield events(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            }
        ).encode()

    async def aclose(self):
        self.closed = True


async def test_stream_delivers_text_before_upstream_finishes(
    key, upstream, monkeypatch
):
    from app.streaming import stream_to_upstream

    stream = DelayedStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        )
    ) as client:
        monkeypatch.setattr("app.services.get_http_client", lambda: client)
        async with AsyncSessionLocal() as db:
            response = await asyncio.wait_for(
                stream_to_upstream(
                    "/v1/chat/completions", {**BASE, "stream": True}, db, key
                ),
                1,
            )
        iterator = response.body_iterator
        await anext(iterator)  # role chunk
        first_text = await asyncio.wait_for(anext(iterator), 1)
        assert (
            json.loads(first_text.split("data: ", 1)[1])["choices"][0]["delta"][
                "content"
            ]
            == "你好"
        )
        assert not stream.release.is_set()
        stream.release.set()
        remaining = "".join([chunk async for chunk in iterator])
        assert '"finish_reason": "stop"' in remaining
        assert stream.closed


async def test_client_cancellation_closes_upstream_and_records_once(
    key, upstream, monkeypatch
):
    from app.streaming import stream_to_upstream

    stream = DelayedStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        )
    ) as client:
        monkeypatch.setattr("app.services.get_http_client", lambda: client)
        async with AsyncSessionLocal() as db:
            response = await stream_to_upstream(
                "/v1/chat/completions", {**BASE, "stream": True}, db, key
            )
        iterator = response.body_iterator
        await anext(iterator)
        await anext(iterator)
        waiting = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await response.background()
        assert stream.closed
    async with AsyncSessionLocal() as db:
        logs = (await db.scalars(select(UsageLog))).all()
        assert len(logs) == 1
        assert logs[0].status_code == 499


@pytest.mark.parametrize(
    "options",
    [
        {"body": "data: not-json\n\n"},
        {
            "body": events(
                {"type": "response.completed", "response": {"output": ["bad"]}}
            )
        },
        {
            "body": events(
                {
                    "type": "response.completed",
                    "response": {"output": [], "usage": {"input_tokens": -1}},
                }
            )
        },
        {"body": "<html>bad gateway</html>", "content_type": "text/html"},
    ],
)
async def test_malformed_upstream_is_502(client, key, upstream, options):
    upstream.update(options)
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    assert response.status_code == 502
    assert "error" in response.json()


async def test_sse_without_content_type_header_is_still_decoded():
    response = httpx.Response(
        200,
        content=events(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_no_content_type",
                    "status": "completed",
                    "output": [],
                    "output_text": "ok",
                },
            }
        ).encode(),
    )
    assert (await read_upstream_data(response))["id"] == "resp_no_content_type"


async def test_untyped_sse_stream_uses_incremental_path(client, key, upstream):
    upstream["content_type"] = ""
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**BASE, "stream": True}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in response.text


async def test_upstream_http_error_is_preserved(client, key, upstream):
    upstream.update(
        status=429,
        content_type="application/json",
        body=json.dumps(
            {"error": {"message": "rate limited", "type": "rate_limit_error"}}
        ),
    )
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"


async def test_body_limit_is_enforced(client, key, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_request_bytes", 20)
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    assert response.status_code == 413


async def test_image_body_uses_extended_limit(client, key, upstream, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_request_bytes", 20)
    monkeypatch.setattr(settings, "max_image_request_bytes", 1024)
    payload = {
        "model": "gpt-5.5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + "eA==" * 30},
                    }
                ],
            }
        ],
    }
    response = await client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert response.status_code == 200


async def test_initial_stream_failure_returns_error_http_status(client, key, upstream):
    upstream["body"] = events(
        {
            "type": "response.failed",
            "response": {"status": "failed", "error": {"message": "failed"}},
        }
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**BASE, "stream": True}
    )
    assert response.status_code == 502
    assert "error" in response.json()


async def test_embeddings_are_forwarded_by_generic_v1_proxy(client, key, upstream):
    upstream.update(content_type="application/json", body=json.dumps({"data": []}))
    response = await client.post(
        "/v1/embeddings",
        headers=AUTH,
        json={"model": "text-embedding-3-small", "input": "hello"},
    )
    assert response.status_code == 200
    assert response.json() == {"data": []}
    assert str(upstream["requests"][-1].url) == "https://chatgpt.com/backend-api/codex/embeddings"


async def test_image_generation_uses_images_endpoint_and_preserves_response(
    client, key, upstream
):
    upstream.update(
        content_type="application/json",
        body=json.dumps({"created": 1, "data": [{"b64_json": "aGVsbG8="}]}),
    )
    payload = {
        "model": "image2",
        "prompt": "a small blue bird",
        "size": "1024x1024",
        "quality": "high",
    }
    response = await client.post(
        "/v1/images/generations", headers=AUTH, json=payload
    )
    assert response.status_code == 200
    assert response.json()["data"][0]["b64_json"] == "aGVsbG8="
    request = upstream["requests"][-1]
    assert str(request.url) == "https://chatgpt.com/backend-api/codex/images/generations"
    assert json.loads(request.content) == payload


async def test_unknown_v1_route_is_authenticated_and_forwarded_verbatim(
    client, key, upstream
):
    upstream.update(
        content_type="application/json",
        body=json.dumps({"ok": True}),
    )
    response = await client.post(
        "/v1/files?purpose=test", headers=AUTH, content=b"raw-body"
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    request = upstream["requests"][-1]
    assert str(request.url) == "https://chatgpt.com/backend-api/codex/files?purpose=test"
    assert request.content == b"raw-body"


async def test_stream_recovers_final_text_and_tool_argument_suffixes(
    client, key, upstream
):
    upstream["body"] = events(
        {"type": "response.output_text.delta", "delta": "hel"},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "weather",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": '{"city":',
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "hello"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "weather",
                        "arguments": '{"city":"Paris"}',
                    },
                ],
            },
        },
    )
    response = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**BASE, "stream": True}
    )
    chunks = parse_sse_events(response.text)
    deltas = [
        choice["delta"] for chunk in chunks for choice in chunk.get("choices", [])
    ]
    assert "".join(delta.get("content", "") for delta in deltas) == "hello"
    assert (
        "".join(
            call["function"].get("arguments", "")
            for delta in deltas
            for call in delta.get("tool_calls", [])
        )
        == '{"city":"Paris"}'
    )


async def test_refusal_deltas_remain_refusals(client, key, upstream):
    upstream["body"] = events(
        {"type": "response.refusal.delta", "delta": "cannot help"},
        {"type": "response.completed", "response": {"status": "completed"}},
    )
    response = await client.post("/v1/chat/completions", headers=AUTH, json=BASE)
    message = response.json()["choices"][0]["message"]
    assert message["refusal"] == "cannot help"
    assert message["content"] is None
