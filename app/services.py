from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import anyio
import httpx
from fastapi import HTTPException, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

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
    normalize_codex_response_to_chat,
    normalize_codex_response_to_responses,
    reject_constant,
    response_failed,
    validate_upstream_data,
)
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
    if error is not None:
        return error
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(504, "upstream request timed out")
    return HTTPException(502, "upstream connection failed")


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


async def _send_upstream(
    path: str, body: dict, bearer: str, account: str
) -> httpx.Response:
    url = f"{settings.upstream_base_url.rstrip('/')}/codex/responses"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "chatgpt-account-id": account,
        "OpenAI-Beta": settings.upstream_openai_beta,
        "originator": settings.oauth_originator,
        "User-Agent": "pi (linux x86_64)",
        "accept": "text/event-stream",
    }
    log_debug("upstream_request", path=path, model=body["model"])
    client = get_http_client()
    response = await client.send(
        client.build_request("POST", url, headers=headers, json=body), stream=True
    )
    log_debug(
        "upstream_response",
        path=path,
        status_code=response.status_code,
        request_id=request_id(response),
    )
    return response


async def open_upstream(
    path: str,
    payload: dict[str, Any],
    db: AsyncSession | None = None,
    upstream_credential_id: int | None = None,
) -> httpx.Response:
    body = build_codex_request_body(path, payload)
    if settings.upstream_auth_mode != "oauth_manual":
        if upstream_credential_id is not None:
            raise HTTPException(503, "account bindings require oauth_manual mode")
        bearer, account = await resolve_upstream_credential(db)
        account = account or extract_account_id_from_token(bearer)
        if not account:
            raise HTTPException(
                502, "upstream credential does not contain a ChatGPT account ID"
            )
        try:
            return await _send_upstream(path, body, bearer, account)
        except httpx.RequestError as exc:
            raise network_error(exc) from None
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
            lease = await wait_for_account(upstream_credential_id, excluded, deadline)
        except TimeoutError:
            raise HTTPException(503, "upstream account scheduler is busy") from None
        except HTTPException as exc:
            if upstream_credential_id is not None:
                exc.upstream_credential_id = upstream_credential_id
            raise
        if lease is None:
            break
        cid = lease.credential_id
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
                exc.upstream_credential_id = cid
                raise
            except HTTPException as exc:
                exc.upstream_credential_id = cid
                last_error = exc
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
                continue
            try:
                response = await _send_upstream(path, body, bearer, account)
            except httpx.RequestError as exc:
                error = network_error(exc)
                error.upstream_credential_id = cid
                if isinstance(error, ProxyTransportError):
                    raise error from None
                await mark_account_failure(
                    cid, settings.upstream_failure_cooldown_seconds
                )
                # Read/write failures may occur after the model received input.
                if isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                ):
                    last_error = error
                    continue
                raise error from None
            response.extensions["gateway_credential_id"] = cid
            response.extensions["gateway_account_lease"] = lease
            response.stream = LeasedStream(response.stream, lease)
            if response.status_code not in (401, 403, 429, 500, 502, 503, 504):
                handed_off = True
                return response
            await mark_account_failure(cid, cooldown_for_response(response))
            if attempt == attempts - 1:
                handed_off = True
                return response
            try:
                await response.aread()
            except httpx.RequestError as exc:
                error = network_error(exc)
                error.upstream_credential_id = cid
                raise error from None
            last_response = response
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


async def iter_upstream_events(response: httpx.Response):
    lines = []
    try:
        async for line in response.aiter_lines():
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
        return status_code if status_code >= 400 else 502, result
    if path == "/v1/chat/completions":
        return status_code, normalize_codex_response_to_chat(data, payload["model"])
    return status_code, normalize_codex_response_to_responses(data)


async def read_upstream_data(response: httpx.Response) -> dict:
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
    try:
        data = response.json(parse_constant=reject_constant)
    except ValueError:
        raise HTTPException(502, "upstream returned a non-JSON response") from None
    if not isinstance(data, dict):
        raise HTTPException(502, "upstream returned a non-object response")
    if response.status_code < 400 and not any(
        key in data for key in ("output", "output_text", "error")
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
        data = await read_upstream_data(response)
        code, result = format_upstream_result(path, payload, response.status_code, data)
        if code >= 400 and response.status_code < 400:
            await mark_account_failure(cid, settings.upstream_failure_cooldown_seconds)
        return code, result, request_id(response), cid
    except HTTPException as exc:
        exc.upstream_credential_id = cid
        if response.status_code < 400 and not isinstance(exc, ProxyTransportError):
            await mark_account_failure(cid, settings.upstream_failure_cooldown_seconds)
        raise
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
            path=path,
            model=model,
            status_code=status_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            upstream_request_id=upstream_request_id,
            upstream_credential_id=upstream_credential_id,
            error_message=error_message,
        )
    )
    await db.commit()


async def parse_request_json(request: Request) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > settings.max_request_bytes:
            raise HTTPException(413, "request body exceeds MAX_REQUEST_BYTES")
        body.extend(chunk)
    try:
        result = json.loads(body, parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "invalid JSON body") from None
    if not isinstance(result, dict):
        raise HTTPException(400, "JSON body must be an object")
    return result
