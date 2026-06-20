from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_auth_schemas import LoginStartResponse
from app.auth import require_admin, require_api_key
from app.codex_auth import codex_auth_manager
from app.config import settings
from app.db import Base, engine, get_db
from app.models import ApiKey, OAuthSession, UpstreamCredential, User
from app.oauth_manual import complete_oauth_session, start_oauth_session
from app.oauth_schemas import OAuthCompleteRequest
from app.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, UserCreate, UserOut
from app.services import forward_to_upstream, parse_request_json, record_usage, resolve_upstream_bearer_token, validate_model
from app.utils import make_api_key, mask_key, sha256_text


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path('data').mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get('/healthz')
async def healthz() -> dict[str, Any]:
    return {'ok': True, 'app': settings.app_name}


@app.post('/admin/users', response_model=UserOut, dependencies=[Depends(require_admin)])
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail='user name already exists')
    user = User(name=payload.name, note=payload.note)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@app.get('/admin/users', response_model=list[UserOut], dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.id.asc()))
    return list(result.scalars().all())


@app.post('/admin/keys', response_model=ApiKeyCreated, dependencies=[Depends(require_admin)])
async def create_key(payload: ApiKeyCreate, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, payload.user_id)
    if not user:
        raise HTTPException(status_code=404, detail='user not found')

    raw_key = make_api_key()
    entity = ApiKey(
        user_id=payload.user_id,
        name=payload.name,
        key_hash=sha256_text(raw_key),
        key_preview=mask_key(raw_key) or '***',
        enabled=True,
    )
    db.add(entity)
    await db.commit()
    await db.refresh(entity)
    return ApiKeyCreated(
        id=entity.id,
        user_id=entity.user_id,
        name=entity.name,
        api_key=raw_key,
        key_preview=entity.key_preview,
        enabled=entity.enabled,
        created_at=entity.created_at,
    )


@app.get('/admin/keys', response_model=list[ApiKeyOut], dependencies=[Depends(require_admin)])
async def list_keys(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).order_by(ApiKey.id.asc()))
    return list(result.scalars().all())


@app.delete('/admin/keys/{key_id}', dependencies=[Depends(require_admin)])
async def delete_key(key_id: int, db: AsyncSession = Depends(get_db)):
    entity = await db.get(ApiKey, key_id)
    if not entity:
        raise HTTPException(status_code=404, detail='key not found')
    await db.delete(entity)
    await db.commit()
    return {'ok': True, 'deleted': key_id}


@app.post('/admin/keys/{key_id}/disable', dependencies=[Depends(require_admin)])
async def disable_key(key_id: int, db: AsyncSession = Depends(get_db)):
    entity = await db.get(ApiKey, key_id)
    if not entity:
        raise HTTPException(status_code=404, detail='key not found')
    entity.enabled = False
    await db.commit()
    return {'ok': True, 'id': key_id, 'enabled': False}


@app.post('/admin/keys/{key_id}/enable', dependencies=[Depends(require_admin)])
async def enable_key(key_id: int, db: AsyncSession = Depends(get_db)):
    entity = await db.get(ApiKey, key_id)
    if not entity:
        raise HTTPException(status_code=404, detail='key not found')
    entity.enabled = True
    await db.commit()
    return {'ok': True, 'id': key_id, 'enabled': True}


@app.get('/admin/usage/summary', dependencies=[Depends(require_admin)])
async def usage_summary(db: AsyncSession = Depends(get_db)):
    stmt = select(
        ApiKey.id,
        ApiKey.user_id,
        ApiKey.name,
        ApiKey.key_preview,
        ApiKey.enabled,
        ApiKey.request_count,
        ApiKey.prompt_tokens,
        ApiKey.completion_tokens,
        ApiKey.total_tokens,
    ).order_by(ApiKey.id.asc())
    result = await db.execute(stmt)
    rows = [dict(row._mapping) for row in result.all()]

    totals_stmt = select(
        func.coalesce(func.sum(ApiKey.request_count), 0),
        func.coalesce(func.sum(ApiKey.prompt_tokens), 0),
        func.coalesce(func.sum(ApiKey.completion_tokens), 0),
        func.coalesce(func.sum(ApiKey.total_tokens), 0),
    )
    totals = (await db.execute(totals_stmt)).one()
    return {
        'items': rows,
        'totals': {
            'request_count': totals[0],
            'prompt_tokens': totals[1],
            'completion_tokens': totals[2],
            'total_tokens': totals[3],
        },
    }


@app.get('/admin/auth/upstream', dependencies=[Depends(require_admin)])
async def get_upstream_auth_status(db: AsyncSession = Depends(get_db)):
    auth_state = codex_auth_manager.login_status()
    latest_credential = None
    cred = (await db.execute(select(UpstreamCredential).where(UpstreamCredential.provider == 'codex').order_by(UpstreamCredential.id.desc()))).scalars().first()
    if cred:
        latest_credential = {
            'id': cred.id,
            'auth_mode': cred.auth_mode,
            'token_type': cred.token_type,
            'scope': cred.scope,
            'account_id': cred.account_id,
            'expires_at': cred.expires_at.isoformat() + 'Z' if cred.expires_at else None,
            'has_refresh_token': bool(cred.refresh_token),
            'preview': f'{cred.access_token[:8]}...{cred.access_token[-4:]}' if cred.access_token and len(cred.access_token) > 16 else None,
        }
    resolved = None
    error = None
    try:
        token = await resolve_upstream_bearer_token(db)
        resolved = {
            'available': True,
            'preview': f'{token[:8]}...{token[-4:]}' if len(token) > 16 else '***',
            'length': len(token),
        }
    except Exception as e:
        error = str(e)
        resolved = {'available': False}
    return {
        'upstream_auth_mode': settings.upstream_auth_mode,
        'upstream_base_url': settings.upstream_base_url,
        'codex': auth_state,
        'oauth': {
            'authorize_url_configured': bool(settings.oauth_authorize_url),
            'token_url_configured': bool(settings.oauth_token_url),
            'client_id_configured': bool(settings.oauth_client_id),
            'redirect_uri': settings.oauth_redirect_uri,
        },
        'latest_credential': latest_credential,
        'resolved': resolved,
        'error': error,
    }


@app.post('/admin/auth/codex/login/start', response_model=LoginStartResponse, dependencies=[Depends(require_admin)])
async def start_codex_login():
    result = codex_auth_manager.start_login()
    return LoginStartResponse(**result)


@app.get('/admin/auth/codex/login/status', dependencies=[Depends(require_admin)])
async def codex_login_status():
    return codex_auth_manager.login_status()


@app.post('/admin/auth/codex/oauth/start', dependencies=[Depends(require_admin)])
async def admin_oauth_start(db: AsyncSession = Depends(get_db)):
    return await start_oauth_session(db)


@app.post('/admin/auth/codex/oauth/complete', dependencies=[Depends(require_admin)])
async def admin_oauth_complete(payload: OAuthCompleteRequest, db: AsyncSession = Depends(get_db)):
    return await complete_oauth_session(db, payload.session_id, payload.callback_url)


@app.get('/admin/auth/codex/oauth/status/{session_id}', dependencies=[Depends(require_admin)])
async def admin_oauth_status(session_id: int, db: AsyncSession = Depends(get_db)):
    entity = await db.get(OAuthSession, session_id)
    if not entity:
        raise HTTPException(status_code=404, detail='oauth session not found')
    return {
        'id': entity.id,
        'provider': entity.provider,
        'status': entity.status,
        'state': entity.state,
        'authorize_url': entity.authorize_url,
        'expires_at': entity.expires_at.isoformat() + 'Z' if entity.expires_at else None,
        'created_at': entity.created_at.isoformat() + 'Z',
        'completed_at': entity.completed_at.isoformat() + 'Z' if entity.completed_at else None,
        'callback_received': bool(entity.callback_url),
    }


@app.post('/admin/auth/codex/logout', dependencies=[Depends(require_admin)])
async def admin_oauth_logout(db: AsyncSession = Depends(get_db)):
    creds = (await db.execute(select(UpstreamCredential).where(UpstreamCredential.provider == 'codex'))).scalars().all()
    for cred in creds:
        await db.delete(cred)
    await db.commit()
    return {'ok': True, 'deleted_credentials': len(creds)}


@app.get('/admin/auth/codex/account', dependencies=[Depends(require_admin)])
async def admin_oauth_account(db: AsyncSession = Depends(get_db)):
    cred = (await db.execute(select(UpstreamCredential).where(UpstreamCredential.provider == 'codex').order_by(UpstreamCredential.id.desc()))).scalars().first()
    if not cred:
        return {'logged_in': False}
    return {
        'logged_in': True,
        'provider': cred.provider,
        'auth_mode': cred.auth_mode,
        'token_type': cred.token_type,
        'scope': cred.scope,
        'account_id': cred.account_id,
        'expires_at': cred.expires_at.isoformat() + 'Z' if cred.expires_at else None,
        'has_refresh_token': bool(cred.refresh_token),
    }


async def handle_proxy(path: str, request: Request, api_key: ApiKey, db: AsyncSession) -> JSONResponse:
    payload = await parse_request_json(request)
    model = payload.get('model')
    validate_model(model)
    status_code, data, req_id = await forward_to_upstream(path, payload, db)
    await record_usage(db, api_key, path, model, status_code, data, req_id)
    return JSONResponse(status_code=status_code, content=data)


@app.post('/v1/chat/completions')
async def proxy_chat(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    return await handle_proxy('/v1/chat/completions', request, api_key, db)


@app.post('/v1/responses')
async def proxy_responses(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    return await handle_proxy('/v1/responses', request, api_key, db)


@app.post('/v1/embeddings')
async def proxy_embeddings(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    return await handle_proxy('/v1/embeddings', request, api_key, db)


@app.post('/v1/models')
async def proxy_models_post():
    raise HTTPException(status_code=405, detail='use GET /v1/models')


@app.get('/v1/models')
async def list_models(api_key: ApiKey = Depends(require_api_key)):
    items = [{'id': m, 'object': 'model'} for m in settings.allowed_models]
    return {'object': 'list', 'data': items}


if __name__ == '__main__':
    uvicorn.run('app.main:app', host=settings.host, port=settings.port, reload=False)
