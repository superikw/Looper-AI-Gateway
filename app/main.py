from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.admin_auth_schemas import LoginStartResponse
from app.auth import require_admin, require_api_key
from app.codex_auth import codex_auth_manager
from app.config import settings
from app.db import engine, get_db
from app.http_client import close_http_client
from app.migrations import initialize_database
from app.model_policy import (
    block_model,
    enforce_model_policy,
    list_blocked_models,
    unblock_model,
    validate_model,
)
from app.models import ApiKey, OAuthSession, User
from app.oauth_manual import (
    active_upstream_credential_query,
    complete_oauth_session,
    logout_upstream_credentials,
    start_oauth_session,
)
from app.oauth_schemas import OAuthCompleteRequest
from app.proxy_schemas import validate_proxy_payload
from app.schemas import (
    ApiKeyBinding,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ModelBlockCreate,
    ModelBlockOut,
    UpstreamAccountUpdate,
    UserCreate,
    UserOut,
)
from app.services import (
    forward_raw_to_upstream,
    forward_to_upstream,
    parse_request_json,
    read_request_body,
    record_usage,
    resolve_upstream_bearer_token,
    resolve_upstream_credential,
)
from app.streaming import stream_to_upstream
from app.token_utils import extract_account_id_from_token
from app.upstreams import list_accounts, pool_is_ready, update_account, validate_binding
from app.utils import make_api_key, mask_key, sha256_text, utcnow


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime()
    Path("data").mkdir(parents=True, exist_ok=True)
    await initialize_database()
    try:
        yield
    finally:
        await close_http_client()
        await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if not request.url.path.startswith("/v1/"):
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )
    message = exc.detail if isinstance(exc.detail, str) else "invalid request"
    error = {
        "message": message,
        "type": "invalid_request_error" if exc.status_code < 500 else "gateway_error",
    }
    if not isinstance(exc.detail, str):
        error["details"] = exc.detail
    return JSONResponse(
        {"error": error}, status_code=exc.status_code, headers=exc.headers
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "app": settings.app_name}


@app.get("/readyz")
async def readyz(db: AsyncSession = Depends(get_db)):
    checks = {"database": False, "upstream_auth": False}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = True
        if settings.upstream_auth_mode == "oauth_manual":
            checks["upstream_auth"] = await pool_is_ready(db)
        else:
            token, account = await resolve_upstream_credential(db)
            checks["upstream_auth"] = bool(
                account or extract_account_id_from_token(token)
            )
    except (HTTPException, SQLAlchemyError, ValueError, OSError):
        pass
    ready = all(checks.values())
    return JSONResponse(
        {"ok": ready, "checks": checks}, status_code=200 if ready else 503
    )


@app.post("/admin/users", response_model=UserOut, dependencies=[Depends(require_admin)])
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="user name already exists")
    user = User(name=payload.name, note=payload.note)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@app.get(
    "/admin/users", response_model=list[UserOut], dependencies=[Depends(require_admin)]
)
async def list_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.id.asc()))
    return list(result.scalars().all())


@app.post(
    "/admin/keys", response_model=ApiKeyCreated, dependencies=[Depends(require_admin)]
)
async def create_key(payload: ApiKeyCreate, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, payload.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    await validate_binding(db, payload.upstream_credential_id)

    raw_key = make_api_key()
    entity = ApiKey(
        user_id=payload.user_id,
        name=payload.name,
        key_hash=sha256_text(raw_key),
        key_preview=mask_key(raw_key) or "***",
        enabled=True,
        upstream_credential_id=payload.upstream_credential_id,
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
        upstream_credential_id=entity.upstream_credential_id,
    )


@app.get(
    "/admin/keys", response_model=list[ApiKeyOut], dependencies=[Depends(require_admin)]
)
async def list_keys(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ApiKey).where(ApiKey.deleted_at.is_(None)).order_by(ApiKey.id.asc())
    )
    return list(result.scalars().all())


@app.patch("/admin/keys/{key_id}/upstream", dependencies=[Depends(require_admin)])
async def bind_key(
    key_id: int, payload: ApiKeyBinding, db: AsyncSession = Depends(get_db)
):
    key = await db.get(ApiKey, key_id)
    if not key or key.deleted_at is not None:
        raise HTTPException(404, "key not found")
    await validate_binding(db, payload.upstream_credential_id)
    key.upstream_credential_id = payload.upstream_credential_id
    await db.commit()
    return {
        "ok": True,
        "id": key_id,
        "upstream_credential_id": key.upstream_credential_id,
    }


@app.get("/admin/upstreams", dependencies=[Depends(require_admin)])
async def upstream_accounts(
    include_deleted: bool = False, db: AsyncSession = Depends(get_db)
):
    return await list_accounts(db, include_deleted)


@app.patch("/admin/upstreams/{credential_id}", dependencies=[Depends(require_admin)])
async def patch_upstream(
    credential_id: int,
    payload: UpstreamAccountUpdate,
    db: AsyncSession = Depends(get_db),
):
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("enabled", True) is None:
        raise HTTPException(400, "enabled must be a boolean")
    await update_account(db, credential_id, changes)
    return {"ok": True, "id": credential_id}


@app.get(
    "/admin/models/blacklist",
    dependencies=[Depends(require_admin)],
    response_model=list[ModelBlockOut],
)
async def model_blacklist(db: AsyncSession = Depends(get_db)):
    return await list_blocked_models(db)


@app.post(
    "/admin/models/blacklist",
    dependencies=[Depends(require_admin)],
    response_model=ModelBlockOut,
)
async def add_blocked_model(
    payload: ModelBlockCreate, db: AsyncSession = Depends(get_db)
):
    return await block_model(db, payload.model)


@app.delete("/admin/models/blacklist", dependencies=[Depends(require_admin)])
async def remove_blocked_model(model: str, db: AsyncSession = Depends(get_db)):
    return {"ok": True, "model": await unblock_model(db, model)}


@app.delete("/admin/upstreams/{credential_id}", dependencies=[Depends(require_admin)])
async def delete_upstream(credential_id: int, db: AsyncSession = Depends(get_db)):
    if not await logout_upstream_credentials(db, credential_id):
        raise HTTPException(404, "upstream account not found")
    return {"ok": True, "deleted": credential_id}


@app.delete("/admin/keys/{key_id}", dependencies=[Depends(require_admin)])
async def delete_key(key_id: int, db: AsyncSession = Depends(get_db)):
    entity = await db.get(ApiKey, key_id)
    if not entity or entity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="key not found")
    entity.enabled = False
    entity.deleted_at = utcnow()
    await db.commit()
    return {"ok": True, "deleted": key_id}


@app.post("/admin/keys/{key_id}/disable", dependencies=[Depends(require_admin)])
async def disable_key(key_id: int, db: AsyncSession = Depends(get_db)):
    entity = await db.get(ApiKey, key_id)
    if not entity or entity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="key not found")
    entity.enabled = False
    await db.commit()
    return {"ok": True, "id": key_id, "enabled": False}


@app.post("/admin/keys/{key_id}/enable", dependencies=[Depends(require_admin)])
async def enable_key(key_id: int, db: AsyncSession = Depends(get_db)):
    entity = await db.get(ApiKey, key_id)
    if not entity or entity.deleted_at is not None:
        raise HTTPException(status_code=404, detail="key not found")
    entity.enabled = True
    await db.commit()
    return {"ok": True, "id": key_id, "enabled": True}


@app.get("/admin/usage/summary", dependencies=[Depends(require_admin)])
async def usage_summary(db: AsyncSession = Depends(get_db)):
    stmt = select(
        ApiKey.id,
        ApiKey.user_id,
        ApiKey.name,
        ApiKey.key_preview,
        ApiKey.enabled,
        ApiKey.deleted_at,
        ApiKey.request_count,
        ApiKey.prompt_tokens,
        ApiKey.completion_tokens,
        ApiKey.total_tokens,
    ).order_by(ApiKey.id.asc())
    result = await db.execute(stmt)
    rows = [
        {
            **dict(row._mapping),
            "deleted_at": row.deleted_at.isoformat() + "Z" if row.deleted_at else None,
        }
        for row in result.all()
    ]

    totals_stmt = select(
        func.coalesce(func.sum(ApiKey.request_count), 0),
        func.coalesce(func.sum(ApiKey.prompt_tokens), 0),
        func.coalesce(func.sum(ApiKey.completion_tokens), 0),
        func.coalesce(func.sum(ApiKey.total_tokens), 0),
    )
    totals = (await db.execute(totals_stmt)).one()
    return {
        "items": rows,
        "totals": {
            "request_count": totals[0],
            "prompt_tokens": totals[1],
            "completion_tokens": totals[2],
            "total_tokens": totals[3],
        },
    }


@app.get("/admin/auth/upstream", dependencies=[Depends(require_admin)])
async def get_upstream_auth_status(db: AsyncSession = Depends(get_db)):
    auth_state = await asyncio.to_thread(codex_auth_manager.login_status)
    latest_credential = None
    cred = (await db.execute(active_upstream_credential_query())).scalars().first()
    if cred:
        latest_credential = {
            "id": cred.id,
            "auth_mode": cred.auth_mode,
            "token_type": cred.token_type,
            "scope": cred.scope,
            "account_id": cred.account_id,
            "expires_at": cred.expires_at.isoformat() + "Z"
            if cred.expires_at
            else None,
            "has_refresh_token": bool(cred.refresh_token),
            "preview": f"{cred.access_token[:8]}...{cred.access_token[-4:]}"
            if cred.access_token and len(cred.access_token) > 16
            else None,
        }
    resolved = None
    error = None
    try:
        token = await resolve_upstream_bearer_token(db)
        resolved = {
            "available": True,
            "preview": f"{token[:8]}...{token[-4:]}" if len(token) > 16 else "***",
            "length": len(token),
        }
    except Exception as e:
        error = str(e)
        resolved = {"available": False}
    return {
        "upstream_auth_mode": settings.upstream_auth_mode,
        "upstream_base_url": settings.upstream_base_url,
        "codex": auth_state,
        "oauth": {
            "authorize_url_configured": bool(settings.oauth_authorize_url),
            "token_url_configured": bool(settings.oauth_token_url),
            "client_id_configured": bool(settings.oauth_client_id),
            "redirect_uri": settings.oauth_redirect_uri,
        },
        "latest_credential": latest_credential,
        "resolved": resolved,
        "error": error,
    }


@app.post(
    "/admin/auth/codex/login/start",
    response_model=LoginStartResponse,
    dependencies=[Depends(require_admin)],
)
async def start_codex_login():
    result = await asyncio.to_thread(codex_auth_manager.start_login)
    return LoginStartResponse(**result)


@app.get("/admin/auth/codex/login/status", dependencies=[Depends(require_admin)])
async def codex_login_status():
    return await asyncio.to_thread(codex_auth_manager.login_status)


@app.post("/admin/auth/codex/oauth/start", dependencies=[Depends(require_admin)])
async def admin_oauth_start(db: AsyncSession = Depends(get_db)):
    return await start_oauth_session(db)


@app.post("/admin/auth/codex/oauth/complete", dependencies=[Depends(require_admin)])
async def admin_oauth_complete(
    payload: OAuthCompleteRequest, db: AsyncSession = Depends(get_db)
):
    return await complete_oauth_session(
        db, payload.session_id, payload.callback_url, payload.name
    )


@app.get(
    "/admin/auth/codex/oauth/status/{session_id}", dependencies=[Depends(require_admin)]
)
async def admin_oauth_status(session_id: int, db: AsyncSession = Depends(get_db)):
    entity = await db.get(OAuthSession, session_id)
    if not entity:
        raise HTTPException(status_code=404, detail="oauth session not found")
    return {
        "id": entity.id,
        "provider": entity.provider,
        "status": entity.status,
        "state": entity.state,
        "authorize_url": entity.authorize_url,
        "expires_at": entity.expires_at.isoformat() + "Z"
        if entity.expires_at
        else None,
        "created_at": entity.created_at.isoformat() + "Z",
        "completed_at": entity.completed_at.isoformat() + "Z"
        if entity.completed_at
        else None,
        "callback_received": bool(entity.callback_url),
    }


@app.post("/admin/auth/codex/logout", dependencies=[Depends(require_admin)])
async def admin_oauth_logout(db: AsyncSession = Depends(get_db)):
    count = await logout_upstream_credentials(db)
    return {"ok": True, "deleted_credentials": count}


@app.get("/admin/auth/codex/account", dependencies=[Depends(require_admin)])
async def admin_oauth_account(db: AsyncSession = Depends(get_db)):
    cred = (await db.execute(active_upstream_credential_query())).scalars().first()
    if not cred:
        return {"logged_in": False}
    return {
        "logged_in": True,
        "provider": cred.provider,
        "auth_mode": cred.auth_mode,
        "token_type": cred.token_type,
        "scope": cred.scope,
        "account_id": cred.account_id,
        "expires_at": cred.expires_at.isoformat() + "Z" if cred.expires_at else None,
        "has_refresh_token": bool(cred.refresh_token),
    }


async def handle_proxy(path: str, request: Request, api_key: ApiKey, db: AsyncSession):
    model = None
    try:
        payload = validate_proxy_payload(path, await parse_request_json(request))
        model = validate_model(payload["model"])
        await enforce_model_policy(db, model)
        payload["model"] = model
        if payload.get("stream", False):
            return await stream_to_upstream(path, payload, db, api_key)
        status_code, data, req_id, cid = await forward_to_upstream(
            path, payload, db, api_key.upstream_credential_id
        )
    except HTTPException as exc:
        await record_usage(
            db,
            api_key,
            path,
            model,
            exc.status_code,
            {"error": {"message": str(exc.detail)}},
            None,
            getattr(exc, "upstream_credential_id", None),
        )
        raise
    await record_usage(db, api_key, path, model, status_code, data, req_id, cid)
    headers = {"x-request-id": req_id} if req_id else None
    return JSONResponse(status_code=status_code, content=data, headers=headers)


def _raw_json(body: bytes, content_type: str) -> dict[str, Any]:
    if "json" not in content_type.lower():
        return {}
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def handle_raw_proxy(
    path: str, request: Request, api_key: ApiKey, db: AsyncSession
):
    model = None
    try:
        body = await read_request_body(request)
        payload = _raw_json(body, request.headers.get("content-type", ""))
        candidate = payload.get("model")
        if isinstance(candidate, str) and candidate.strip():
            model = validate_model(candidate)
            await enforce_model_policy(db, model)
        status_code, content, req_id, cid, content_type = await forward_raw_to_upstream(
            path,
            body,
            request.method,
            request.url.query,
            {
                "content-type": request.headers.get("content-type", ""),
                "accept": request.headers.get("accept", "*/*"),
            },
            db,
            api_key.upstream_credential_id,
        )
    except HTTPException as exc:
        await record_usage(
            db,
            api_key,
            path,
            model,
            exc.status_code,
            {"error": {"message": str(exc.detail)}},
            None,
            getattr(exc, "upstream_credential_id", None),
        )
        raise

    result = _raw_json(content, content_type or "")
    await record_usage(db, api_key, path, model, status_code, result, req_id, cid)
    headers = {}
    if content_type:
        headers["content-type"] = content_type
    if req_id:
        headers["x-request-id"] = req_id
    return Response(content=content, status_code=status_code, headers=headers)


@app.post("/v1/chat/completions")
async def proxy_chat(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    return await handle_proxy("/v1/chat/completions", request, api_key, db)


@app.post("/v1/responses")
async def proxy_responses(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    return await handle_proxy("/v1/responses", request, api_key, db)


@app.post("/v1/images/generations")
async def proxy_image_generations(
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    return await handle_proxy("/v1/images/generations", request, api_key, db)


@app.get("/v1/models")
async def list_models(
    api_key: ApiKey = Depends(require_api_key), db: AsyncSession = Depends(get_db)
):
    blocked = {item.model for item in await list_blocked_models(db)}
    items = [
        {"id": m, "object": "model"}
        for m in settings.catalog_models
        if m not in blocked
    ]
    return {"object": "list", "data": items}


@app.api_route(
    "/v1/{subpath:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_other_v1_routes(
    subpath: str,
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    if not subpath:
        raise HTTPException(status_code=404, detail="v1 route is required")
    return await handle_raw_proxy(f"/v1/{subpath}", request, api_key, db)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
