from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import OAuthSession, UpstreamCredential

JWT_CLAIM_PATH = 'https://api.openai.com/auth'


@dataclass
class PkceBundle:
    verifier: str
    challenge: str
    state: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')


def make_pkce_bundle() -> PkceBundle:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode('utf-8')).digest())
    state = secrets.token_hex(16)
    return PkceBundle(verifier=verifier, challenge=challenge, state=state)


def _now() -> datetime:
    return datetime.utcnow()


def ensure_oauth_configured() -> None:
    missing = []
    if not settings.oauth_authorize_url:
        missing.append('OAUTH_AUTHORIZE_URL')
    if not settings.oauth_token_url:
        missing.append('OAUTH_TOKEN_URL')
    if not settings.oauth_client_id:
        missing.append('OAUTH_CLIENT_ID')
    if not settings.oauth_redirect_uri:
        missing.append('OAUTH_REDIRECT_URI')
    if missing:
        raise HTTPException(status_code=500, detail=f'missing oauth config: {", ".join(missing)}')


def build_authorize_url(bundle: PkceBundle) -> str:
    ensure_oauth_configured()
    params = {
        'response_type': 'code',
        'client_id': settings.oauth_client_id,
        'redirect_uri': settings.oauth_redirect_uri,
        'scope': settings.oauth_scope,
        'state': bundle.state,
        'code_challenge': bundle.challenge,
        'code_challenge_method': 'S256',
        'originator': settings.oauth_originator,
    }
    if settings.oauth_audience:
        params['audience'] = settings.oauth_audience
    if settings.oauth_id_token_add_organizations:
        params['id_token_add_organizations'] = 'true'
    if settings.oauth_codex_cli_simplified_flow:
        params['codex_cli_simplified_flow'] = 'true'
    return f"{settings.oauth_authorize_url}?{urlencode(params)}"


async def start_oauth_session(db: AsyncSession) -> dict[str, Any]:
    bundle = make_pkce_bundle()
    auth_url = build_authorize_url(bundle)
    entity = OAuthSession(
        provider='codex',
        state=bundle.state,
        code_verifier=bundle.verifier,
        authorize_url=auth_url,
        status='pending',
        expires_at=_now() + timedelta(minutes=settings.oauth_session_ttl_minutes),
    )
    db.add(entity)
    await db.commit()
    await db.refresh(entity)
    return {
        'session_id': entity.id,
        'auth_url': auth_url,
        'state': bundle.state,
        'expires_at': entity.expires_at.isoformat() + 'Z',
        'instructions': '在浏览器打开 auth_url，完成登录后，把最终跳转后的完整 callback URL 粘贴到 /admin/auth/codex/oauth/complete',
    }


def parse_callback_url(callback_url: str) -> dict[str, str]:
    parsed = urlparse(callback_url)
    params = parse_qs(parsed.query)
    flat = {k: v[0] for k, v in params.items() if v}
    if 'error' in flat:
        raise HTTPException(status_code=400, detail=f"oauth error: {flat.get('error')} {flat.get('error_description', '')}".strip())
    if 'code' not in flat:
        raise HTTPException(status_code=400, detail='callback url missing code')
    if 'state' not in flat:
        raise HTTPException(status_code=400, detail='callback url missing state')
    return flat


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


def extract_account_id(access_token: str, token_data: dict[str, Any]) -> str | None:
    direct = token_data.get('account_id') or token_data.get('sub')
    if isinstance(direct, str) and direct:
        return direct
    payload = _decode_jwt_payload(access_token)
    if not payload:
        return None
    auth_claim = payload.get(JWT_CLAIM_PATH)
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get('chatgpt_account_id')
        if isinstance(account_id, str) and account_id:
            return account_id
    return None


async def complete_oauth_session(db: AsyncSession, session_id: int, callback_url: str) -> dict[str, Any]:
    entity = await db.get(OAuthSession, session_id)
    if not entity:
        raise HTTPException(status_code=404, detail='oauth session not found')
    if entity.status != 'pending':
        raise HTTPException(status_code=409, detail=f'oauth session already {entity.status}')
    if entity.expires_at and entity.expires_at < _now():
        entity.status = 'expired'
        await db.commit()
        raise HTTPException(status_code=410, detail='oauth session expired')

    parsed = parse_callback_url(callback_url)
    if parsed['state'] != entity.state:
        raise HTTPException(status_code=400, detail='oauth state mismatch')

    token_data = await exchange_code_for_token(parsed['code'], entity.code_verifier)
    access_token = token_data.get('access_token')
    refresh_token = token_data.get('refresh_token')
    if not access_token:
        raise HTTPException(status_code=502, detail='token endpoint did not return access_token')
    if not refresh_token:
        raise HTTPException(status_code=502, detail='token endpoint did not return refresh_token')

    expires_in = int(token_data.get('expires_in') or 3600)
    expires_at = _now() + timedelta(seconds=expires_in)
    account_id = extract_account_id(access_token, token_data)

    existing = await db.execute(select(UpstreamCredential).where(UpstreamCredential.provider == 'codex').order_by(UpstreamCredential.id.desc()))
    current = existing.scalars().first()
    if not current:
        current = UpstreamCredential(provider='codex')
        db.add(current)

    current.auth_mode = 'oauth_manual'
    current.access_token = access_token
    current.refresh_token = refresh_token
    current.token_type = token_data.get('token_type', 'Bearer')
    current.scope = token_data.get('scope')
    current.expires_at = expires_at
    current.account_id = account_id
    current.raw_json = json.dumps(token_data, ensure_ascii=False)
    current.updated_at = _now()

    entity.status = 'completed'
    entity.callback_url = callback_url
    entity.completed_at = _now()

    await db.commit()
    await db.refresh(current)
    return {
        'ok': True,
        'session_id': session_id,
        'credential_id': current.id,
        'expires_at': expires_at.isoformat() + 'Z',
        'token_type': current.token_type,
        'scope': current.scope,
        'account_id': current.account_id,
    }


async def exchange_code_for_token(code: str, code_verifier: str) -> dict[str, Any]:
    ensure_oauth_configured()
    payload = {
        'grant_type': 'authorization_code',
        'client_id': settings.oauth_client_id,
        'code': code,
        'redirect_uri': settings.oauth_redirect_uri,
        'code_verifier': code_verifier,
    }
    if settings.oauth_client_secret:
        payload['client_secret'] = settings.oauth_client_secret

    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
        resp = await client.post(settings.oauth_token_url, data=payload, headers=headers)
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f'token endpoint returned non-json body: {resp.text[:400]}')
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f'token exchange failed: {data}')
    return data


async def refresh_upstream_credential(db: AsyncSession, credential: UpstreamCredential) -> UpstreamCredential:
    if not credential.refresh_token:
        raise HTTPException(status_code=401, detail='upstream oauth credential has no refresh_token; please login again')
    ensure_oauth_configured()
    payload = {
        'grant_type': 'refresh_token',
        'client_id': settings.oauth_client_id,
        'refresh_token': credential.refresh_token,
    }
    if settings.oauth_client_secret:
        payload['client_secret'] = settings.oauth_client_secret
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
        resp = await client.post(settings.oauth_token_url, data=payload, headers=headers)
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f'refresh endpoint returned non-json body: {resp.text[:400]}')
    if resp.status_code >= 400:
        raise HTTPException(status_code=401, detail=f'oauth refresh failed: {data}')

    credential.access_token = data.get('access_token') or credential.access_token
    credential.refresh_token = data.get('refresh_token') or credential.refresh_token
    credential.token_type = data.get('token_type', credential.token_type or 'Bearer')
    credential.scope = data.get('scope') or credential.scope
    credential.expires_at = _now() + timedelta(seconds=int(data.get('expires_in') or 3600))
    credential.account_id = extract_account_id(credential.access_token, data) or credential.account_id
    credential.raw_json = json.dumps(data, ensure_ascii=False)
    credential.updated_at = _now()
    await db.commit()
    await db.refresh(credential)
    return credential


async def get_active_upstream_credential(db: AsyncSession) -> UpstreamCredential | None:
    result = await db.execute(select(UpstreamCredential).where(UpstreamCredential.provider == 'codex').order_by(UpstreamCredential.id.desc()))
    credential = result.scalars().first()
    if not credential:
        return None
    if credential.expires_at and credential.expires_at <= _now() + timedelta(seconds=90):
        if credential.refresh_token:
            credential = await refresh_upstream_credential(db, credential)
    return credential
