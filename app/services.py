from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.codex_auth import CodexAuthError, codex_auth_manager
from app.config import settings
from app.models import ApiKey, UsageLog
from app.oauth_manual import get_active_upstream_credential

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


def _convert_content_to_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{'type': 'input_text', 'text': content}]
    parts: list[dict[str, Any]] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get('type')
            if block_type in ('text', 'input_text', 'output_text'):
                text = block.get('text') or block.get('content') or ''
                if text:
                    parts.append({'type': 'input_text', 'text': text})
    return parts


def _convert_messages_to_input(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    items: list[dict[str, Any]] = []
    system_prompt: str | None = None
    for msg in messages:
        role = msg.get('role')
        content = msg.get('content')
        if role in ('system', 'developer'):
            parts = _convert_content_to_parts(content)
            text = '\n'.join(p['text'] for p in parts if p.get('text'))
            if text:
                system_prompt = f"{system_prompt}\n{text}".strip() if system_prompt else text
            continue

        if role == 'assistant':
            assistant_parts = []
            if isinstance(content, str):
                assistant_parts = [{'type': 'output_text', 'text': content, 'annotations': []}]
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get('type')
                    if block_type in ('text', 'output_text', 'input_text'):
                        text = block.get('text') or block.get('content') or ''
                        if text:
                            assistant_parts.append({'type': 'output_text', 'text': text, 'annotations': []})
            if assistant_parts:
                items.append({
                    'type': 'message',
                    'role': 'assistant',
                    'status': 'completed',
                    'content': assistant_parts,
                })
            continue

        parts = _convert_content_to_parts(content)
        if not parts:
            continue
        items.append({'role': 'user', 'content': parts})
    return items, system_prompt


def build_codex_request_body(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get('model')
    body: dict[str, Any] = {
        'model': model,
        'store': False,
        'stream': True,
        'text': {'verbosity': 'medium'},
        'include': ['reasoning.encrypted_content'],
        'tool_choice': 'auto',
        'parallel_tool_calls': True,
    }

    if path == '/v1/chat/completions':
        messages = payload.get('messages') or []
        input_items, system_prompt = _convert_messages_to_input(messages)
        body['input'] = input_items
        body['instructions'] = system_prompt or settings.default_instructions
    elif path == '/v1/responses':
        input_value = payload.get('input')
        if isinstance(input_value, str):
            body['input'] = [{'role': 'user', 'content': [{'type': 'input_text', 'text': input_value}]}]
        elif isinstance(input_value, list):
            body['input'] = input_value
        else:
            body['input'] = []
        body['instructions'] = payload.get('instructions') or settings.default_instructions
    else:
        raise HTTPException(status_code=400, detail=f'unsupported codex backend path: {path}')

    if payload.get('temperature') is not None:
        body['temperature'] = payload.get('temperature')
    if payload.get('max_output_tokens') is not None:
        body['max_output_tokens'] = payload.get('max_output_tokens')
    if payload.get('reasoning') is not None:
        body['reasoning'] = payload.get('reasoning')
    if payload.get('metadata') is not None:
        body['metadata'] = payload.get('metadata')
    return body


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for chunk in text.split('\n\n'):
        lines = [line[5:].strip() for line in chunk.splitlines() if line.startswith('data:')]
        if not lines:
            continue
        data = '\n'.join(lines).strip()
        if not data or data == '[DONE]':
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def extract_sse_json(text: str) -> dict[str, Any] | None:
    candidates = parse_sse_events(text)
    delta_text_parts: list[str] = []
    done_text_parts: list[str] = []
    for obj in candidates:
        event_type = obj.get('type')
        if event_type in ('response.output_text.delta', 'response.refusal.delta'):
            delta = obj.get('delta')
            if isinstance(delta, str) and delta:
                delta_text_parts.append(delta)
        elif event_type == 'response.output_item.done':
            item = obj.get('item')
            if isinstance(item, dict) and item.get('type') == 'message':
                for content in item.get('content', []) or []:
                    if isinstance(content, dict):
                        if content.get('type') == 'output_text' and isinstance(content.get('text'), str):
                            done_text_parts.append(content['text'])
                        elif content.get('type') == 'refusal' and isinstance(content.get('refusal'), str):
                            done_text_parts.append(content['refusal'])
    aggregated_text = ''.join(delta_text_parts) if delta_text_parts else ''.join(done_text_parts)
    for obj in reversed(candidates):
        event_type = obj.get('type')
        if event_type in ('response.completed', 'response.done') and isinstance(obj.get('response'), dict):
            response = dict(obj['response'])
            if aggregated_text and not response.get('output_text'):
                response['output_text'] = aggregated_text
            return response
    for obj in reversed(candidates):
        if isinstance(obj, dict) and ('output' in obj or 'output_text' in obj or 'usage' in obj):
            response = dict(obj)
            if aggregated_text and not response.get('output_text'):
                response['output_text'] = aggregated_text
            return response
    if aggregated_text:
        return {'output_text': aggregated_text}
    return None


def normalize_codex_response_to_chat(data: dict[str, Any], model: str) -> dict[str, Any]:
    output_text = data.get('output_text')
    if not output_text:
        parts: list[str] = []
        for item in data.get('output', []) or []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'message':
                for content in item.get('content', []) or []:
                    if isinstance(content, dict):
                        text = content.get('text') or content.get('output_text')
                        if text:
                            parts.append(text)
            elif item.get('type') in ('output_text', 'text'):
                text = item.get('text') or item.get('output_text')
                if text:
                    parts.append(text)
        output_text = ''.join(parts)
    if not output_text and isinstance(data.get('error'), dict):
        output_text = data['error'].get('message', '')

    usage = data.get('usage') or {}
    prompt_tokens = int(usage.get('input_tokens') or usage.get('prompt_tokens') or 0)
    completion_tokens = int(usage.get('output_tokens') or usage.get('completion_tokens') or 0)
    total_tokens = int(usage.get('total_tokens') or (prompt_tokens + completion_tokens))

    return {
        'id': data.get('id', 'codex_response'),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': {'role': 'assistant', 'content': output_text or ''},
            'finish_reason': 'stop',
        }],
        'usage': {
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': total_tokens,
        },
        '_codex_raw': data,
    }


def normalize_codex_response_to_responses(data: dict[str, Any]) -> dict[str, Any]:
    usage = data.get('usage') or {}
    return {
        'id': data.get('id'),
        'object': 'response',
        'model': data.get('model'),
        'output': data.get('output', []),
        'output_text': data.get('output_text', ''),
        'usage': {
            'input_tokens': int(usage.get('input_tokens') or 0),
            'output_tokens': int(usage.get('output_tokens') or 0),
            'total_tokens': int(usage.get('total_tokens') or 0),
        },
        '_codex_raw': data,
    }


async def forward_to_upstream(path: str, payload: dict[str, Any], db: AsyncSession | None = None) -> tuple[int, dict[str, Any], str | None]:
    bearer_token, account_id = await resolve_upstream_credential(db)
    account_id = account_id or extract_account_id_from_token(bearer_token)
    if not account_id:
        raise HTTPException(status_code=500, detail='failed to extract chatgpt_account_id from token')

    if path == '/v1/embeddings':
        raise HTTPException(status_code=400, detail='embeddings are not supported in codex backend mode')

    codex_body = build_codex_request_body(path, payload)
    url = f"{settings.upstream_base_url.rstrip('/')}/codex/responses"
    headers = {
        'Authorization': f'Bearer {bearer_token}',
        'chatgpt-account-id': account_id,
        'OpenAI-Beta': settings.upstream_openai_beta,
        'originator': settings.oauth_originator,
        'User-Agent': 'pi (linux x86_64)',
        'accept': 'text/event-stream',
        'content-type': 'application/json',
    }

    log_debug(
        'upstream_request',
        target='codex_backend',
        path=path,
        url=url,
        model=payload.get('model'),
        account_id=account_id,
        request_body=codex_body,
        headers={
            'chatgpt-account-id': account_id,
            'OpenAI-Beta': settings.upstream_openai_beta,
            'originator': settings.oauth_originator,
            'accept': headers['accept'],
            'content-type': headers['content-type'],
        },
    )

    async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
        resp = await client.post(url, headers=headers, json=codex_body)

    req_id = resp.headers.get('x-request-id') or resp.headers.get('openai-request-id')
    raw_text = resp.text
    content_type = resp.headers.get('content-type', '')

    data: dict[str, Any]
    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        data = parsed
    else:
        sse_obj = extract_sse_json(raw_text)
        if isinstance(sse_obj, dict):
            data = sse_obj
        else:
            data = {
                'error': {
                    'message': raw_text[:2000],
                    'content_type': content_type,
                    'status_code': resp.status_code,
                }
            }

    log_debug(
        'upstream_response',
        target='codex_backend',
        path=path,
        url=url,
        status_code=resp.status_code,
        request_id=req_id,
        content_type=content_type,
        response_preview=raw_text[:2000],
        parsed_keys=list(data.keys()) if isinstance(data, dict) else None,
        output_text_preview=(data.get('output_text')[:200] if isinstance(data, dict) and isinstance(data.get('output_text'), str) else None),
    )

    if resp.status_code >= 400:
        return resp.status_code, data, req_id

    if path == '/v1/chat/completions':
        return resp.status_code, normalize_codex_response_to_chat(data, payload.get('model') or data.get('model') or ''), req_id
    if path == '/v1/responses':
        return resp.status_code, normalize_codex_response_to_responses(data), req_id
    return resp.status_code, data, req_id


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

    api_key.request_count += 1
    api_key.prompt_tokens += prompt_tokens
    api_key.completion_tokens += completion_tokens
    api_key.total_tokens += total_tokens

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


def validate_model(model: str | None) -> None:
    allowed = settings.allowed_models
    if allowed and model and model not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f'model not allowed: {model}',
        )


async def parse_request_json(request: Request) -> dict[str, Any]:
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='invalid json body')
