from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from sqlalchemy import update
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.codex_auth import CodexAuthError, codex_auth_manager
from app.config import settings
from app.model_utils import canonicalize_model_name
from app.models import ApiKey, UsageLog
from app.oauth_manual import get_active_upstream_credential
from app.http_client import get_http_client
from app.protocol import (build_codex_request_body, parse_sse_events, extract_sse_json,
    normalize_codex_response_to_chat, normalize_codex_response_to_responses,
    decode_sse_data, ResponseAccumulator, response_failed, validate_upstream_data, reject_constant)

JWT_CLAIM_PATH = 'https://api.openai.com/auth'


def log_debug(event: str, **kwargs: Any) -> None:
    payload = {'event': event, **kwargs}
    try:
        print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
    except Exception:
        print(str(payload), flush=True)


async def resolve_upstream_credential(db: AsyncSession | None = None) -> tuple[str, str | None]:
    mode = settings.upstream_auth_mode.strip().lower() or 'oauth_manual'
    if mode == 'env_token':
        if not settings.upstream_bearer_token:
            raise HTTPException(status_code=500, detail='UPSTREAM_BEARER_TOKEN is not configured')
        return settings.upstream_bearer_token, None
    if mode == 'codex_auth_file':
        try:
            return codex_auth_manager.load_bearer_token(), None
        except CodexAuthError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
    if mode == 'oauth_manual':
        if db is None:
            raise HTTPException(status_code=500, detail='database session required for oauth_manual mode')
        credential = await get_active_upstream_credential(db)
        if not credential or not credential.access_token:
            raise HTTPException(status_code=401, detail='no active oauth credential; start login first')
        return credential.access_token, credential.account_id

    if settings.upstream_bearer_token:
        return settings.upstream_bearer_token, None
    if db is not None:
        credential = await get_active_upstream_credential(db)
        if credential and credential.access_token:
            return credential.access_token, credential.account_id
    try:
        return codex_auth_manager.load_bearer_token(), None
    except CodexAuthError as e:
        raise HTTPException(status_code=500, detail=f'no usable upstream auth found: {e}') from e


async def resolve_upstream_bearer_token(db: AsyncSession | None = None) -> str:
    token, _ = await resolve_upstream_credential(db)
    return token


def _decode_jwt_payload(access_token: str) -> dict[str, Any] | None:
    try:
        parts = access_token.split('.')
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = '=' * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode('utf-8')
        obj = json.loads(decoded)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def extract_account_id_from_token(access_token: str) -> str | None:
    payload = _decode_jwt_payload(access_token)
    if not payload:
        return None
    auth_claim = payload.get(JWT_CLAIM_PATH)
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get('chatgpt_account_id')
        if isinstance(account_id, str) and account_id:
            return account_id
    return None


def network_error(exc: httpx.RequestError) -> HTTPException:
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(504, 'upstream request timed out')
    return HTTPException(502, 'upstream connection failed')


async def open_upstream(path: str, payload: dict[str, Any], db: AsyncSession | None = None) -> httpx.Response:
    body = build_codex_request_body(path, payload)
    bearer, account = await resolve_upstream_credential(db)
    account = account or extract_account_id_from_token(bearer)
    if not account:
        raise HTTPException(502, 'upstream credential does not contain a ChatGPT account ID')
    url = f"{settings.upstream_base_url.rstrip('/')}/codex/responses"
    headers = {'Authorization':f'Bearer {bearer}', 'chatgpt-account-id':account,
               'OpenAI-Beta':settings.upstream_openai_beta, 'originator':settings.oauth_originator,
               'User-Agent':'pi (linux x86_64)', 'accept':'text/event-stream'}
    log_debug('upstream_request', path=path, model=body['model'])
    client = get_http_client()
    try:
        response = await client.send(client.build_request('POST', url, headers=headers, json=body), stream=True)
    except httpx.RequestError as exc:
        raise network_error(exc) from None
    log_debug('upstream_response', path=path, status_code=response.status_code, request_id=request_id(response))
    return response


def request_id(response: httpx.Response) -> str | None:
    return response.headers.get('x-request-id') or response.headers.get('openai-request-id')


async def iter_upstream_events(response: httpx.Response):
    lines = []
    try:
        async for line in response.aiter_lines():
            if not line:
                event = decode_sse_data(lines)
                lines = []
                if event is not None:
                    yield event
            elif line.startswith('data:'):
                lines.append(line[5:].removeprefix(' '))
        event = decode_sse_data(lines)
        if event is not None:
            yield event
    except httpx.RequestError as exc:
        raise network_error(exc) from None


def format_upstream_result(path: str, payload: dict, status_code: int, data: dict) -> tuple[int, dict]:
    validate_upstream_data(data)
    if status_code >= 400 or response_failed(data):
        error = data.get('error') or {'message':'upstream response failed', 'type':'upstream_error'}
        if not isinstance(error, dict):
            error = {'message':str(error), 'type':'upstream_error'}
        result = {'error':error}
        if data.get('usage'):
            result['usage'] = data['usage']
        return status_code if status_code >= 400 else 502, result
    if path == '/v1/chat/completions':
        return status_code, normalize_codex_response_to_chat(data, payload['model'])
    return status_code, normalize_codex_response_to_responses(data)


async def read_upstream_data(response: httpx.Response) -> dict:
    if 'text/event-stream' in response.headers.get('content-type', '') and response.status_code < 400:
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
        raise HTTPException(502, 'upstream returned a non-JSON response') from None
    if not isinstance(data, dict):
        raise HTTPException(502, 'upstream returned a non-object response')
    if response.status_code < 400 and not any(key in data for key in ('output', 'output_text', 'error')):
        raise HTTPException(502, 'upstream returned an invalid response')
    return data


async def forward_to_upstream(path: str, payload: dict[str, Any], db: AsyncSession | None = None) -> tuple[int, dict[str, Any], str | None]:
    response = await open_upstream(path, payload, db)
    try:
        data = await read_upstream_data(response)
        code, result = format_upstream_result(path, payload, response.status_code, data)
        return code, result, request_id(response)
    finally:
        await response.aclose()




async def record_usage(
    db: AsyncSession,
    api_key: ApiKey,
    path: str,
    model: str | None,
    status_code: int,
    response_json: dict[str, Any],
    upstream_request_id: str | None,
) -> None:
    usage = response_json.get('usage') or {}
    prompt_tokens = int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
    completion_tokens = int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)
    total_tokens = int(usage.get('total_tokens') or (prompt_tokens + completion_tokens))

    await db.execute(update(ApiKey).where(ApiKey.id == api_key.id).values(
        request_count=ApiKey.request_count + 1,
        prompt_tokens=ApiKey.prompt_tokens + prompt_tokens,
        completion_tokens=ApiKey.completion_tokens + completion_tokens,
        total_tokens=ApiKey.total_tokens + total_tokens,
    ).execution_options(synchronize_session=False))

    error_message = None
    if status_code >= 400:
        error = response_json.get('error') if isinstance(response_json, dict) else None
        if isinstance(error, dict):
            error_message = error.get('message') or json.dumps(error, ensure_ascii=False)
        else:
            error_message = str(error) if error else None

    db.add(UsageLog(
        api_key_id=api_key.id,
        user_id=api_key.user_id,
        path=path,
        model=model,
        status_code=status_code,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        upstream_request_id=upstream_request_id,
        error_message=error_message,
    ))
    await db.commit()


def validate_model(model: str | None) -> str | None:
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, 'model must be a non-empty string')
    normalized_model = canonicalize_model_name(model)
    allowed = settings.allowed_models
    if allowed and normalized_model and normalized_model not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f'model not allowed: {model}',
        )
    return normalized_model


async def parse_request_json(request: Request) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_request_bytes:
            raise HTTPException(413, 'request body exceeds MAX_REQUEST_BYTES')
    try:
        result = json.loads(body, parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'invalid JSON body') from None
    if not isinstance(result, dict):
        raise HTTPException(400, 'JSON body must be an object')
    return result
