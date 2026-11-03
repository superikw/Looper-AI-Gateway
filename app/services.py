from __future__ import annotations

import asyncio
import codecs
import json
import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import anyio
import httpx
from fastapi import HTTPException, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.attempts import (
    RequestTrace,
    current_trace,
    finish_response,
    result_category,
    safe_identifier,
)
from app.codex_auth import CodexAuthError, codex_auth_manager
from app.config import settings
from app.db import AsyncSessionLocal
from app.http_client import ProxyTransportError, get_http_client, proxy_transport_error
from app.models import ApiKey, UpstreamCredential, UsageLog
from app.oauth_manual import get_active_upstream_credential
from app.protocol import (
    ResponseAccumulator,
    build_codex_request_body,
    decode_sse_data,
    extract_sse_json,
    normalize_codex_response_to_chat,
    normalize_codex_response_to_responses,
    reject_constant,
    response_failed,
    validate_upstream_data,
)
from app.sse_preview import Preview, retryable_failure
from app.token_utils import extract_account_id_from_token
from app.upstreams import AccountLease, mark_account_failure, wait_for_account


def log_debug(event: str, **kwargs: Any) -> None:
    payload = {"event": event, **kwargs}
    try:
        print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
    except Exception:
        print(str(payload), flush=True)


async def resolve_upstream_credential(
    db: AsyncSession | None = None,
) -> tuple[str, str | None]:
    mode = settings.upstream_auth_mode
    if mode == "env_token":
        if not settings.upstream_bearer_token:
            raise HTTPException(
                status_code=500, detail="UPSTREAM_BEARER_TOKEN is not configured"
            )
        return settings.upstream_bearer_token, None
    if mode == "codex_auth_file":
        try:
            return codex_auth_manager.load_bearer_token(), None
        except CodexAuthError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
    if mode != "oauth_manual":
        raise HTTPException(500, "unsupported upstream authentication mode")
    if db is None:
        raise HTTPException(
            status_code=500,
            detail="database session required for oauth_manual mode",
        )
    credential = await get_active_upstream_credential(db)
    if not credential or not credential.access_token:
        raise HTTPException(
            status_code=401, detail="no active oauth credential; start login first"
        )
    return credential.access_token, credential.account_id


async def resolve_upstream_bearer_token(db: AsyncSession | None = None) -> str:
    token, _ = await resolve_upstream_credential(db)
    return token


def network_error(exc: httpx.RequestError) -> HTTPException:
    error = proxy_transport_error(exc)
    if error is None:
        error = (
            HTTPException(504, "upstream request timed out")
            if isinstance(exc, httpx.TimeoutException)
            else HTTPException(502, "upstream connection failed")
        )
    error.failure_category = (
        "proxy_transport"
        if isinstance(error, ProxyTransportError)
        else "network_transport"
    )
    error.transport_exception_type = type(exc).__name__
    return error


def account_id_for_response(response: httpx.Response) -> int | None:
    return response.extensions.get("gateway_credential_id")


class LeasedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, lease: AccountLease):
        self.stream, self.lease = stream, lease

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            await self.lease.release()


async def close_upstream(response: httpx.Response) -> None:
    # Cached/mock responses may already be closed before the lease is attached.
    with anyio.CancelScope(shield=True):
        try:
            preview = response.extensions.get("gateway_preview")
            try:
                if preview is not None:
                    await preview.close()
            finally:
                await response.aclose()
        finally:
            lease = response.extensions.get("gateway_account_lease")
            if lease is not None:
                await lease.release()


def cooldown_for_response(response: httpx.Response) -> int:
    if response.status_code in (401, 403):
        return 300
    if response.status_code == 429:
        value = response.headers.get("retry-after", "")
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (
                    parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)
                ).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = settings.upstream_rate_limit_cooldown_seconds
        if not math.isfinite(delay):
            delay = settings.upstream_rate_limit_cooldown_seconds
        return max(1, min(math.ceil(delay), 86400))
    return settings.upstream_failure_cooldown_seconds


def should_cooldown_for_result(result: dict[str, Any]) -> bool:
    """Preserve account cooldown rules without treating overload as a ban.

    Overload can differ by account/model. Its bounded model health policy is a
    separate concern; absence of an account ban does not imply global failure.
    """
    if retryable_failure(result):
        return False
    error = result.get("error") if isinstance(result, dict) else None
    if not isinstance(error, dict):
        return True
    message = str(error.get("message") or "").lower()
    return "overloaded" not in message


async def _send_upstream(
    path: str,
    body: dict,
    bearer: str,
    account: str,
    *,
    method: str = "POST",
    raw_body: bytes | None = None,
    query: str = "",
    request_headers: dict[str, str] | None = None,
) -> httpx.Response:
    upstream_path = (
        "/codex/responses"
        if raw_body is None and path in ("/v1/chat/completions", "/v1/responses")
        else f"/codex{path.removeprefix('/v1')}"
    )
    url = f"{settings.upstream_base_url.rstrip('/')}{upstream_path}"
    if query:
        url = f"{url}?{query}"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "chatgpt-account-id": account,
        "OpenAI-Beta": settings.upstream_openai_beta,
        "originator": settings.oauth_originator,
        "User-Agent": "pi (linux x86_64)",
        "accept": (
            "application/json"
            if path.startswith("/v1/images/")
            else "text/event-stream"
        ),
    }
    for name, value in (request_headers or {}).items():
        if value and name.lower() not in {"authorization", "host", "content-length"}:
            headers[name] = value
    log_debug("upstream_request", path=path, model=body.get("model"))
    client = get_http_client()
    request_kwargs = {"content": raw_body} if raw_body is not None else {"json": body}
    response = await client.send(
        client.build_request(method, url, headers=headers, **request_kwargs),
        stream=True,
    )
    log_debug(
        "upstream_response",
        path=path,
        status_code=response.status_code,
        request_id=request_id(response),
        content_type=response.headers.get("content-type"),
        content_length=response.headers.get("content-length"),
    )
    return response


async def open_upstream(
    path: str,
    payload: dict[str, Any],
    db: AsyncSession | None = None,
    upstream_credential_id: int | None = None,
    *,
    raw_body: bytes | None = None,
    method: str = "POST",
    query: str = "",
    request_headers: dict[str, str] | None = None,
) -> httpx.Response:
    body = {} if raw_body is not None else build_codex_request_body(path, payload)

    async def send(bearer: str, account: str) -> httpx.Response:
        # Keep the simple call shape for existing transports and test doubles;
        # only raw passthrough requests need the extended options.
        if raw_body is None and method == "POST" and not query and not request_headers:
            return await _send_upstream(path, body, bearer, account)
        return await _send_upstream(
            path,
            body,
            bearer,
            account,
            method=method,
            raw_body=raw_body,
            query=query,
            request_headers=request_headers,
        )

    trace = current_trace.get() or RequestTrace()
    if settings.upstream_auth_mode != "oauth_manual":
        if db is not None and db.info.get("route_user_id") is not None:
            raise HTTPException(503, "user account routing requires oauth_manual mode")
        if upstream_credential_id is not None:
            raise HTTPException(503, "account bindings require oauth_manual mode")
        bearer, account = await resolve_upstream_credential(db)
        account = account or extract_account_id_from_token(bearer)
        if not account:
            raise HTTPException(
                502, "upstream credential does not contain a ChatGPT account ID"
            )
        diagnostic = trace.begin(None, path, payload.get("model"))
        try:
            response = await send(bearer, account)
            diagnostic.http_status = response.status_code
            diagnostic.request_id = request_id(response)
            response.extensions["gateway_attempt"] = diagnostic
            return response
        except httpx.RequestError as exc:
            error = network_error(exc)
            diagnostic.finish(error.failure_category, "return_error", exc=error)
            raise error from None
    if db is None:
        raise HTTPException(500, "database session required for oauth_manual mode")
    excluded: set[int] = set()
    last_response = None
    last_error = None
    attempts = (
        1 if upstream_credential_id is not None else settings.upstream_max_attempts
    )
    deadline = (
        asyncio.get_running_loop().time() + settings.upstream_capacity_wait_seconds
    )
    for attempt in range(attempts):
        try:
            lease = await wait_for_account(
                upstream_credential_id, excluded, deadline, db.info.get("route_user_id")
            )
        except TimeoutError:
            if last_response is not None:
                return last_response
            trace.begin(None, path, payload.get("model")).finish(
                "local_capacity", "reject"
            )
            raise HTTPException(503, "upstream account scheduler is busy") from None
        except HTTPException as exc:
            if last_response is not None:
                return last_response
            trace.begin(None, path, payload.get("model")).finish(
                "local_capacity", "reject", exc=exc
            )
            if upstream_credential_id is not None:
                exc.upstream_credential_id = upstream_credential_id
            raise
        if lease is None:
            break
        cid = lease.credential_id
        diagnostic = trace.begin(cid, path, payload.get("model"))
        excluded.add(cid)
        response = None
        handed_off = False
        try:
            try:
                async with AsyncSessionLocal() as credential_db:
                    credential = await get_active_upstream_credential(
                        credential_db, cid
                    )
                    if not credential:
                        raise HTTPException(
                            503, "selected upstream account is unavailable"
                        )
                    bearer = credential.access_token
                    account = credential.account_id or extract_account_id_from_token(
                        bearer
                    )
                    if not account:
                        raise HTTPException(
                            502,
                            "upstream credential does not contain a ChatGPT account ID",
                        )
            except ProxyTransportError as exc:
                diagnostic.finish("proxy_transport", "return_error", exc=exc)
                exc.upstream_credential_id = cid
                raise
            except HTTPException as exc:
                exc.upstream_credential_id = cid
                last_error = exc
                diagnostic.finish("upstream_http", "retry_next_account", exc=exc)
                await mark_account_failure(
                    cid,
                    300
                    if exc.status_code == 401
                    else settings.upstream_failure_cooldown_seconds,
                )
                continue
            try:
                await lease.start_request(deadline)
            except HTTPException as exc:
                exc.upstream_credential_id = cid
                last_error = exc
                diagnostic.finish("local_capacity", "return_error", exc=exc)
                continue
            try:
                response = await send(bearer, account)
            except httpx.RequestError as exc:
                error = network_error(exc)
                error.upstream_credential_id = cid
                if isinstance(error, ProxyTransportError):
                    diagnostic.finish("proxy_transport", "return_error", exc=error)
                    raise error from None
                # Read/write failures may occur after the model received input.
                if isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                ):
                    diagnostic.finish(
                        "network_transport", "retry_next_account", exc=error
                    )
                    last_error = error
                    continue
                diagnostic.finish("network_transport", "return_error", exc=error)
                raise error from None
            diagnostic.http_status = response.status_code
            diagnostic.request_id = request_id(response)
            response.extensions["gateway_attempt"] = diagnostic
            response.extensions["gateway_credential_id"] = cid
            response.extensions["gateway_account_lease"] = lease
            response.stream = LeasedStream(response.stream, lease)
            if response.status_code not in (401, 403, 429, 500, 502, 503, 504):
                if (
                    settings.sse_early_retry
                    and raw_body is None
                    and path in ("/v1/responses", "/v1/chat/completions")
                    and response.status_code == 200
                    and "text/event-stream" in response.headers.get("content-type", "")
                ):
                    preview = Preview(response)
                    response.extensions["gateway_preview"] = preview
                    failure = await preview.scan(
                        settings.sse_preview_max_events,
                        settings.sse_preview_max_bytes,
                        settings.sse_preview_max_seconds,
                    )
                    if failure is not None and attempt < attempts - 1:
                        diagnostic.request_id = (
                            diagnostic.request_id or upstream_request_id(failure)
                        )
                        diagnostic.finish(
                            "sse_business", "retry_next_account", result=failure
                        )
                        # A buffered error snapshot survives closing the old socket.
                        last_response = httpx.Response(
                            200,
                            headers={
                                "content-type": "application/json",
                                **(
                                    {
                                        "x-request-id": safe_identifier(
                                            diagnostic.request_id
                                        )
                                    }
                                    if safe_identifier(diagnostic.request_id)
                                    else {}
                                ),
                            },
                            json=failure,
                            extensions={
                                "gateway_credential_id": cid,
                                "gateway_attempt": diagnostic,
                            },
                        )
                        continue
                handed_off = True
                return response
            try:
                await response.aread()
            except httpx.RequestError as exc:
                error = network_error(exc)
                error.upstream_credential_id = cid
                diagnostic.finish(error.failure_category, "return_error", exc=error)
                raise error from None
            try:
                error_result = response.json()
            except ValueError:
                error_result = {}
            if not isinstance(error_result, dict):
                error_result = {}
            diagnostic.request_id = diagnostic.request_id or upstream_request_id(
                error_result
            )
            # Overload is model/business pressure, not a long account ban.
            if response.status_code in (401, 403) or should_cooldown_for_result(
                error_result
            ):
                await mark_account_failure(cid, cooldown_for_response(response))
            diagnostic.finish(
                "upstream_http",
                "retry_next_account" if attempt < attempts - 1 else "return_error",
                result=error_result,
            )
            if attempt == attempts - 1:
                handed_off = True
                return response
            last_response = response
        except httpx.RequestError as exc:
            error = network_error(exc)
            error.upstream_credential_id = cid
            diagnostic.finish(error.failure_category, "return_error", exc=error)
            raise error from None
        except asyncio.CancelledError:
            diagnostic.finish("client_cancelled", "aborted")
            raise
        except HTTPException as exc:
            diagnostic.finish(
                getattr(exc, "failure_category", "sse_protocol"),
                "return_error",
                exc=exc,
            )
            exc.upstream_credential_id = cid
            raise
        except Exception:
            diagnostic.finish("gateway_internal", "aborted")
            raise
        finally:
            if not handed_off:
                if response is not None:
                    await close_upstream(response)
                else:
                    await lease.release()
    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise HTTPException(
        503,
        "no available upstream accounts; check bindings, enabled state and cooldowns",
    )


def request_id(response: httpx.Response) -> str | None:
    return response.headers.get("x-request-id") or response.headers.get(
        "openai-request-id"
    )


_REQUEST_ID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def upstream_request_id(data: dict[str, Any]) -> str | None:
    """Find a provider request ID when it was returned in the JSON/SSE body."""
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    for value in (
        data.get("request_id"),
        data.get("requestId"),
        error.get("request_id"),
        error.get("requestId"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in (error.get("message"), data.get("message")):
        if isinstance(value, str):
            match = _REQUEST_ID_RE.search(value)
            if match:
                return match.group(0)
    return None


async def iter_upstream_events(response: httpx.Response):
    lines = []
    try:
        preview = response.extensions.get("gateway_preview")

        async def preview_lines():
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            buffer = ""
            async for chunk in preview.replay():
                buffer += decoder.decode(chunk)
                while (match := re.search(r"\r\n|\r(?!$)|\n", buffer)) is not None:
                    yield buffer[: match.start()]
                    buffer = buffer[match.end() :]
            buffer += decoder.decode(b"", final=True)
            for line in buffer.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                yield line

        source = preview_lines() if preview is not None else response.aiter_lines()
        async for line in source:
            if not line:
                event = decode_sse_data(lines)
                lines = []
                if event is not None:
                    yield event
            elif line.startswith("data:"):
                lines.append(line[5:].removeprefix(" "))
        event = decode_sse_data(lines)
        if event is not None:
            yield event
    except httpx.RequestError as exc:
        raise network_error(exc) from None


def format_upstream_result(
    path: str, payload: dict, status_code: int, data: dict
) -> tuple[int, dict]:
    if path not in ("/v1/images/generations", "/v1/images/edits"):
        validate_upstream_data(data)
    if status_code >= 400 or response_failed(data):
        error = data.get("error") or {
            "message": "upstream response failed",
            "type": "upstream_error",
        }
        if not isinstance(error, dict):
            error = {"message": str(error), "type": "upstream_error"}
        result = {"error": error}
        if data.get("usage"):
            result["usage"] = data["usage"]
        provider_request_id = upstream_request_id(data)
        result["upstream"] = {
            "status_code": status_code,
            "request_id": provider_request_id,
        }
        if data.get("status"):
            result["upstream"]["response_status"] = data["status"]
        return status_code if status_code >= 400 else 502, result
    if path in ("/v1/images/generations", "/v1/images/edits"):
        return status_code, data
    if path == "/v1/chat/completions":
        return status_code, normalize_codex_response_to_chat(data, payload["model"])
    return status_code, normalize_codex_response_to_responses(data)


async def read_upstream_data(response: httpx.Response, path: str | None = None) -> dict:
    if (
        "text/event-stream" in response.headers.get("content-type", "")
        and response.status_code < 400
    ):
        accumulator = ResponseAccumulator()
        async for event in iter_upstream_events(response):
            accumulator.add(event)
            if accumulator.terminal:
                break
        return accumulator.result()
    try:
        await response.aread()
    except httpx.RequestError as exc:
        raise network_error(exc) from None
    if response.status_code < 400:
        raw_text = response.content.decode("utf-8", errors="replace")
        has_sse_data = raw_text.lstrip().startswith("data:") or "\ndata:" in raw_text
        if has_sse_data:
            return extract_sse_json(raw_text)
    try:
        data = response.json(parse_constant=reject_constant)
    except ValueError:
        content_type = response.headers.get("content-type", "unknown")
        raise HTTPException(
            502,
            f"upstream returned a non-JSON response (status={response.status_code}, content_type={content_type}, body_length={len(response.content)})",
        ) from None
    if not isinstance(data, dict):
        raise HTTPException(502, "upstream returned a non-object response")
    if (
        response.status_code < 400
        and path not in ("/v1/images/generations", "/v1/images/edits")
        and not any(key in data for key in ("output", "output_text", "error"))
    ):
        raise HTTPException(502, "upstream returned an invalid response")
    return data


async def forward_to_upstream(
    path: str,
    payload: dict[str, Any],
    db: AsyncSession | None = None,
    upstream_credential_id: int | None = None,
) -> tuple[int, dict[str, Any], str | None, int | None]:
    response = await open_upstream(path, payload, db, upstream_credential_id)
    cid = account_id_for_response(response)
    try:
        data = await read_upstream_data(response, path)
        code, result = format_upstream_result(path, payload, response.status_code, data)
        attempt = response.extensions.get("gateway_attempt")
        if attempt:
            attempt.request_id = attempt.request_id or upstream_request_id(data)
        finish_response(response, result_category(response, data), result=result)
        if (
            code >= 400
            and response.status_code < 400
            and should_cooldown_for_result(result)
        ):
            await mark_account_failure(cid, settings.upstream_failure_cooldown_seconds)
        return code, result, request_id(response) or upstream_request_id(data), cid
    except HTTPException as exc:
        finish_response(
            response, getattr(exc, "failure_category", "sse_protocol"), exc=exc
        )
        exc.upstream_credential_id = cid
        if (
            response.status_code < 400
            and not isinstance(exc, ProxyTransportError)
            and not getattr(exc, "transport_exception_type", None)
        ):
            await mark_account_failure(cid, settings.upstream_failure_cooldown_seconds)
        raise
    finally:
        await close_upstream(response)


async def forward_raw_to_upstream(
    path: str,
    body: bytes,
    method: str = "POST",
    query: str = "",
    request_headers: dict[str, str] | None = None,
    db: AsyncSession | None = None,
    upstream_credential_id: int | None = None,
) -> tuple[int, bytes, str | None, int | None, str | None]:
    """Forward an authenticated /v1 request without protocol conversion."""
    response = await open_upstream(
        path,
        {},
        db,
        upstream_credential_id,
        raw_body=body,
        method=method,
        query=query,
        request_headers=request_headers,
    )
    cid = account_id_for_response(response)
    try:
        try:
            await response.aread()
        except httpx.RequestError as exc:
            error = network_error(exc)
            finish_response(response, error.failure_category, exc=error)
            raise error from None
        finish_response(
            response, "upstream_http" if response.status_code >= 400 else None
        )
        return (
            response.status_code,
            response.content,
            request_id(response),
            cid,
            response.headers.get("content-type"),
        )
    finally:
        await close_upstream(response)


async def record_usage(
    db: AsyncSession,
    api_key: ApiKey,
    path: str,
    model: str | None,
    status_code: int,
    response_json: dict[str, Any],
    upstream_request_id: str | None,
    upstream_credential_id: int | None = None,
) -> None:
    usage = response_json.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(
        usage.get("completion_tokens") or usage.get("output_tokens") or 0
    )
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    from app.pricing import ensure_model_price

    price = await ensure_model_price(db, model)
    cost_usd = 0.0
    if price:
        details = (
            usage.get("input_tokens_details")
            or usage.get("prompt_tokens_details")
            or {}
        )
        cached = int(
            usage.get("cached_input_tokens")
            or (details.get("cached_tokens") if isinstance(details, dict) else 0)
            or 0
        )
        cached = min(max(cached, 0), prompt_tokens)
        billable_prompt = max(prompt_tokens - cached, 0)
        cost_usd = (
            billable_prompt * price.input_usd_per_million
            + cached * price.cached_input_usd_per_million
            + completion_tokens * price.output_usd_per_million
        ) / 1_000_000

    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == api_key.id)
        .values(
            request_count=ApiKey.request_count + 1,
            prompt_tokens=ApiKey.prompt_tokens + prompt_tokens,
            completion_tokens=ApiKey.completion_tokens + completion_tokens,
            total_tokens=ApiKey.total_tokens + total_tokens,
        )
        .execution_options(synchronize_session=False)
    )

    if upstream_credential_id is not None:
        await db.execute(
            update(UpstreamCredential)
            .where(UpstreamCredential.id == upstream_credential_id)
            .values(
                prompt_tokens=UpstreamCredential.prompt_tokens + prompt_tokens,
                completion_tokens=UpstreamCredential.completion_tokens
                + completion_tokens,
                total_tokens=UpstreamCredential.total_tokens + total_tokens,
            )
            .execution_options(synchronize_session=False)
        )

    error_message = None
    if status_code >= 400:
        error = response_json.get("error") if isinstance(response_json, dict) else None
        if isinstance(error, dict):
            error_message = error.get("message") or json.dumps(
                error, ensure_ascii=False
            )
        else:
            error_message = str(error) if error else None

    db.add(
        UsageLog(
            api_key_id=api_key.id,
            user_id=api_key.user_id,
            gateway_request_id=current_trace.get().id if current_trace.get() else None,
            path=path,
            model=model,
            status_code=status_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            upstream_request_id=upstream_request_id,
            upstream_credential_id=upstream_credential_id,
            error_message=error_message,
            cost_usd=cost_usd,
        )
    )
    await db.commit()


IMAGE_REQUEST_PATHS = {
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/images/generations",
    "/v1/images/edits",
}
IMAGE_ENDPOINT_PATHS = {"/v1/images/generations", "/v1/images/edits"}
IMAGE_BODY_MARKERS = (
    b'"image_url"',
    b'"input_image"',
    b'"data:image/',
    b'"image_generation"',
)


def request_body_limit(path: str | None = None) -> tuple[int, str]:
    if path in IMAGE_REQUEST_PATHS:
        return settings.max_image_request_bytes, "MAX_IMAGE_REQUEST_BYTES"
    return settings.max_request_bytes, "MAX_REQUEST_BYTES"


async def parse_request_json(
    request: Request, path: str | None = None
) -> dict[str, Any]:
    body = await read_request_body(request, path=path)
    try:
        result = json.loads(body, parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "invalid JSON body") from None
    if not isinstance(result, dict):
        raise HTTPException(400, "JSON body must be an object")
    return result


async def read_request_body(request: Request, *, path: str | None = None) -> bytes:
    limit, setting_name = request_body_limit(path)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, f"request body exceeds {setting_name}")
        body.extend(chunk)
    result = bytes(body)
    # Chat and Responses requests only need the larger limit when they carry
    # an image. Keep the ordinary limit for oversized text/tool payloads while
    # allowing base64 image data URLs through.
    if (
        path in IMAGE_REQUEST_PATHS - IMAGE_ENDPOINT_PATHS
        and len(result) > settings.max_request_bytes
        and not any(marker in result for marker in IMAGE_BODY_MARKERS)
    ):
        raise HTTPException(413, "request body exceeds MAX_REQUEST_BYTES")
    return result
