"""Content-free request/attempt diagnostics; never accept client-supplied IDs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import traceback
from contextvars import ContextVar
from dataclasses import dataclass, field

import anyio
from starlette.requests import ClientDisconnect

from app.db import AsyncSessionLocal
from app.models import UpstreamAttempt

logger = logging.getLogger(__name__)
current_trace: ContextVar[RequestTrace | None] = ContextVar(
    "gateway_trace", default=None
)


class GatewayInternalError(RuntimeError):
    """Safe propagated failure for ASGI servers that log uncaught exceptions."""


def new_request_id():
    value = secrets.token_hex(8)
    return value[:8] + "-" + value[8:]


def display_request_id(request_id, retry_count):
    if not request_id:
        return None
    return f"{request_id}/{retry_count}" if retry_count is not None else request_id


def safe_identifier(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,128}", value)
        else None
    )


def internal_error(exc, request_id):
    # Stack frames without exception messages or locals: provider/library errors
    # may include a URL, headers, or request content in their string form.
    logger.error(
        "gateway internal error request_id=%s exception_type=%s\n%s",
        request_id,
        type(exc).__name__,
        "".join(
            f"{frame.filename}:{frame.lineno} in {frame.name}\n"
            for frame in traceback.extract_tb(exc.__traceback__)
        ),
    )


@dataclass
class Attempt:
    trace: RequestTrace
    number: int
    cid: int | None
    path: str
    model: str | None
    started: float = field(default_factory=time.monotonic)
    http_status: int | None = None
    request_id: str | None = None
    category: str | None = None
    decision: str | None = None
    error_type: str | None = None
    error_code: str | None = None
    transport_type: str | None = None
    elapsed: int = 0
    status_code: int = 500
    usage: dict = field(default_factory=dict)
    error_message: str | None = None
    log_id: int | None = None

    def finish(self, category, decision, *, result=None, exc=None):
        if self.decision is not None:
            return
        self.category, self.decision = category, decision
        self.status_code = (
            getattr(exc, "status_code", None)
            or (
                self.http_status
                if self.http_status and self.http_status >= 400
                else None
            )
            or {
                "sse_business": 502,
                "upstream_business": 502,
                "missing_terminal": 502,
                "sse_protocol": 502,
                "client_cancelled": 499,
                "gateway_internal": 500,
                "local_capacity": 429,
            }.get(category, 200 if category is None else 502)
        )
        self.usage = (result or {}).get("usage") or {}
        error = (result or {}).get("error")
        if isinstance(error, dict):
            self.error_message = str(error.get("message") or "upstream request failed")[
                :2048
            ]
            self.error_type = safe_identifier(error.get("type"))
            self.error_code = safe_identifier(error.get("code"))
        if exc is not None:
            self.error_message = str(getattr(exc, "detail", "upstream request failed"))[
                :2048
            ]
            self.transport_type = safe_identifier(
                getattr(exc, "transport_exception_type", None)
            )
        self.elapsed = round((time.monotonic() - self.started) * 1000)
        print(
            json.dumps(
                {
                    "event": "upstream_attempt",
                    **self.row(),
                    "display_request_id": display_request_id(
                        self.trace.id, self.number - 1
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def row(self):
        return dict(
            gateway_request_id=self.trace.id,
            attempt=self.number,
            retry_count=self.number - 1,
            upstream_credential_id=self.cid,
            path=self.path,
            model=self.model,
            upstream_http_status=self.http_status,
            upstream_request_id=safe_identifier(self.request_id),
            failure_category=self.category,
            upstream_error_type=self.error_type,
            upstream_error_code=self.error_code,
            transport_exception_type=self.transport_type,
            retry_decision=self.decision,
            elapsed_ms=self.elapsed,
        )

    async def persist_usage(self):
        if self.trace.api_key is None or self.log_id is not None:
            return
        from app.services import record_usage

        with anyio.CancelScope(shield=True):
            async with AsyncSessionLocal() as db:
                await record_usage(
                    db,
                    self.trace.api_key,
                    self.path,
                    self.model,
                    self.status_code,
                    {"usage": self.usage, "error": {"message": self.error_message}},
                    self.request_id,
                    self.cid,
                    attempt_override=self,
                    is_final=False,
                )


@dataclass
class RequestTrace:
    id: str = field(default_factory=new_request_id)
    attempts: list[Attempt] = field(default_factory=list)
    api_key: object | None = None
    request_counted: bool = False
    final_attempt: Attempt | None = None

    def begin(self, cid, path, model):
        attempt = Attempt(self, len(self.attempts) + 1, cid, path, model)
        self.attempts.append(attempt)
        return attempt


def response_attempt(response):
    return response.extensions.get("gateway_attempt")


def finish_response(response, category=None, decision="return", result=None, exc=None):
    attempt = response_attempt(response)
    if attempt:
        attempt.trace.final_attempt = attempt
        if not attempt.request_id and isinstance(result, dict):
            for source in (result, result.get("error"), result.get("upstream")):
                if isinstance(source, dict):
                    attempt.request_id = safe_identifier(source.get("request_id"))
                    if attempt.request_id:
                        break
        attempt.finish(category, decision, result=result, exc=exc)


def result_category(response, result):
    if response.status_code >= 400:
        return "upstream_http"
    error = result.get("error") or {}
    if (
        isinstance(error, dict)
        and error.get("message")
        == "upstream stream ended before a terminal response event"
    ):
        return "missing_terminal"
    if result.get("error") or result.get("status") in ("failed", "cancelled"):
        preview = response.extensions.get("gateway_preview")
        return (
            "upstream_business"
            if preview is not None and preview.body_format == "json"
            else "sse_business"
        )
    return None


class RequestTraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/"):
            return await self.app(scope, receive, send)
        trace = RequestTrace()
        scope["gateway_request_id"] = trace.id
        token = current_trace.set(trace)
        category = "gateway_internal"
        status = None

        async def traced_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {
                    **message,
                    "headers": list(message.get("headers", []))
                    + [(b"x-gateway-request-id", trace.id.encode())],
                }
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
            category = "local_capacity" if status == 429 else "gateway_internal"
        except (asyncio.CancelledError, ClientDisconnect):
            category = "client_cancelled"
            raise
        except Exception as exc:
            internal_error(exc, trace.id)
            # Keep the failure observable without letting an ASGI server print
            # arbitrary library exception messages containing request secrets.
            raise GatewayInternalError(
                f"gateway internal error request_id={trace.id}"
            ) from None
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    if not trace.attempts and status in (429, 503):
                        trace.begin(None, scope["path"], None).finish(
                            "local_capacity", "reject"
                        )
                    for attempt in trace.attempts:
                        if attempt.decision is None:
                            attempt.finish(category, "aborted")
                    if trace.attempts:
                        async with AsyncSessionLocal() as db:
                            db.add_all(
                                UpstreamAttempt(**attempt.row())
                                for attempt in trace.attempts
                            )
                            await db.commit()
                except Exception as exc:
                    internal_error(exc, trace.id)
                finally:
                    current_trace.reset(token)
