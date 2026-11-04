"""Forward native Responses bytes; observe metadata without rewriting the stream."""

import asyncio
import json
import re

import anyio
import httpx
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.activity import finish_active_session
from app.attempts import finish_response, result_category
from app.db import AsyncSessionLocal
from app.protocol import reject_constant
from app.services import (
    close_upstream,
    network_error,
    open_upstream,
    record_usage,
    request_id,
    upstream_request_id,
)

_SEPARATOR = re.compile(rb"\r\n\r\n|\n\n|\r\r")
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def forwarding_headers(headers, *, response=False):
    blocked = _HOP_HEADERS | {
        name.strip().lower() for name in headers.get("connection", "").split(",")
    }
    blocked |= {
        "authorization",
        "cookie",
        "chatgpt-account-id",
        "host",
        "x-gateway-request-id",
    }
    if response:
        # httpx yields decoded content. Do not advertise its old wire encoding.
        blocked |= {"content-encoding", "set-cookie"}
    return {
        name: value for name, value in headers.items() if name.lower() not in blocked
    }


class ResponseObserver:
    """Bounded, best-effort inspection; parsing never controls forwarded bytes."""

    def __init__(self, content_type, inspect_terminal=True, on_failure=None):
        self.kind = "sse" if "text/event-stream" in content_type.lower() else None
        self.buffer = b""
        self.skipping = False
        self.too_large = False
        self.limited = False
        self.terminal = False
        self.inspect_terminal = inspect_terminal
        self.result = {}
        self.on_failure = on_failure

    def observe_failure(self):
        if self.on_failure is not None:
            self.on_failure()

    def accept(self, value):
        if not isinstance(value, dict):
            return
        # Keep only accounting/diagnostic fields, never conversation output.
        self.result = {
            key: value[key]
            for key in ("id", "status", "request_id", "error")
            if key in value
        }
        if value.get("error") or value.get("status") in ("failed", "cancelled"):
            self.observe_failure()
        usage = value.get("usage")
        if isinstance(usage, dict):
            self.result["usage"] = {
                key: number
                for key, number in usage.items()
                if type(number) is int and number >= 0
            }
            for key in ("input_tokens_details", "prompt_tokens_details"):
                details = usage.get(key)
                if (
                    isinstance(details, dict)
                    and type(details.get("cached_tokens")) is int
                ):
                    self.result["usage"][key] = {
                        "cached_tokens": max(0, details["cached_tokens"])
                    }

    def event(self, value):
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        if kind in (
            "response.completed",
            "response.done",
            "response.failed",
            "response.incomplete",
            "response.cancelled",
        ):
            self.terminal = True
            self.accept(value.get("response"))
            if kind in ("response.failed", "response.cancelled"):
                self.observe_failure()
                self.result["status"] = "failed"
                self.result.setdefault("error", {"message": "upstream response failed"})
        elif kind == "error":
            self.terminal = True
            self.accept(
                {
                    "status": "failed",
                    "error": value.get("error")
                    or {
                        "message": value.get("message", "upstream error"),
                        "code": value.get("code"),
                    },
                }
            )

    def feed(self, chunk):
        for offset in range(0, len(chunk), 16384):
            self._feed(chunk[offset : offset + 16384])

    def _feed(self, chunk):
        if self.too_large:
            return
        self.buffer += chunk
        if self.kind is None:
            prefix = self.buffer.lstrip(b" \t\r\n")
            if prefix and b"\xef\xbb\xbf".startswith(prefix):
                return
            prefix = prefix.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n")
            if prefix:
                self.kind = "json" if prefix[:1] in (b"{", b"[") else "sse"
        if self.kind == "sse":
            while (match := _SEPARATOR.search(self.buffer)) is not None:
                frame, self.buffer = (
                    self.buffer[: match.start()],
                    self.buffer[match.end() :],
                )
                if self.skipping:
                    self.skipping = False
                    continue
                data = "\n".join(
                    line[5:].removeprefix(" ")
                    for line in frame.decode("utf-8-sig", errors="replace").splitlines()
                    if line.startswith("data:")
                )
                if data and data != "[DONE]":
                    try:
                        self.event(json.loads(data, parse_constant=reject_constant))
                    except (ValueError, TypeError):
                        pass
        if len(self.buffer) > 1024 * 1024:
            self.limited = True
            if self.kind == "sse":
                self.buffer = self.buffer[-3:]
                self.skipping = True
            else:
                self.buffer = b""
                self.too_large = True

    def finish(self):
        if self.kind == "json" and not self.too_large:
            try:
                self.accept(json.loads(self.buffer, parse_constant=reject_constant))
                self.terminal = True
            except (ValueError, TypeError):
                pass
        elif self.kind == "sse" and self.buffer:
            self.feed(b"\n\n")
        if (
            self.inspect_terminal
            and self.kind == "sse"
            and not self.terminal
            and not self.limited
        ):
            self.result = {
                "status": "failed",
                "error": {
                    "message": "upstream stream ended before a terminal response event"
                },
            }
        self.buffer = b""


async def native_to_upstream(path, payload, body, request, db, api_key, active_id):
    response = await open_upstream(
        path,
        payload,
        db,
        api_key.upstream_credential_id,
        raw_body=body,
        method=request.method,
        query=request.url.query,
        request_headers=forwarding_headers(request.headers),
        native=path == "/v1/responses",
    )
    cid = response.extensions.get("gateway_credential_id")
    rid = request_id(response)
    attempt = response.extensions.get("gateway_attempt")
    observer = ResponseObserver(
        response.headers.get("content-type", ""),
        path == "/v1/responses",
        on_failure=attempt.observe_failure if attempt is not None else None,
    )
    state = {
        "recorded": False,
        "code": 500,
        "category": "gateway_internal",
        "data": {"error": {"message": "gateway internal error"}},
    }

    def failed(code, category, message):
        if category == "client_cancelled" and observer.terminal:
            return
        if not state["recorded"]:
            state.update(
                code=code, category=category, data={"error": {"message": message}}
            )

    async def cleanup():
        nonlocal rid
        if state["recorded"]:
            return
        state["recorded"] = True
        with anyio.CancelScope(shield=True):
            rid = rid or upstream_request_id(state["data"])
            attempt = response.extensions.get("gateway_attempt")
            if attempt is not None:
                attempt.request_id = attempt.request_id or rid
            finish_response(response, state["category"], result=state["data"])
            try:
                await close_upstream(response)
            finally:
                try:
                    async with AsyncSessionLocal() as usage_db:
                        await record_usage(
                            usage_db,
                            api_key,
                            path,
                            payload.get("model"),
                            state["code"],
                            state["data"],
                            rid,
                            cid,
                        )
                finally:
                    await finish_active_session(
                        active_id, "completed" if state["code"] < 400 else "failed"
                    )

    async def content():
        try:
            preview = response.extensions.get("gateway_preview")
            if preview is not None:
                source = preview.replay()
            elif response.is_stream_consumed:

                async def cached():
                    yield response.content

                source = cached()
            else:
                source = response.aiter_bytes()
            async for chunk in source:
                observer.feed(chunk)
                if observer.terminal:
                    category = result_category(response, observer.result)
                    state.update(
                        code=response.status_code
                        if response.status_code >= 400 or category is None
                        else 502,
                        category=category,
                        data=observer.result,
                    )
                yield chunk
            observer.finish()
            category = result_category(response, observer.result)
            state.update(
                code=response.status_code
                if response.status_code >= 400 or category is None
                else 502,
                category=category,
                data=observer.result,
            )
        except asyncio.CancelledError:
            failed(499, "client_cancelled", "client disconnected")
            raise
        except httpx.RequestError as exc:
            error = network_error(exc)
            failed(error.status_code, error.failure_category, error.detail)
            finish_response(response, error.failure_category, exc=error)
            raise error from None
        except Exception:
            failed(500, "gateway_internal", "gateway internal error")
            raise
        finally:
            await cleanup()

    class NativeStreamingResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            async def checked_send(message):
                try:
                    await send(message)
                except OSError:
                    failed(499, "client_cancelled", "client disconnected")
                    raise ClientDisconnect() from None

            try:
                await super().__call__(scope, receive, checked_send)
            except (asyncio.CancelledError, ClientDisconnect):
                failed(499, "client_cancelled", "client disconnected")
                raise
            except Exception:
                if state["category"] != "client_cancelled":
                    failed(500, "gateway_internal", "gateway internal error")
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    try:
                        await self.body_iterator.aclose()
                    finally:
                        await cleanup()

    return NativeStreamingResponse(
        content(),
        status_code=response.status_code,
        headers=forwarding_headers(response.headers, response=True),
    )
