from __future__ import annotations

import os
import re
import shutil
import time as monotonic_time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psutil
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_service import (
    create_session,
    hash_password,
    new_initial_password,
    require_admin_session,
    revoke_admin_sessions,
    verify_password,
)
from app.admin_upstreams import router as upstream_router
from app.attempts import display_request_id
from app.config import settings
from app.db import AsyncSessionLocal, get_db
from app.migrations import initialize_database
from app.models import (
    ActiveSession,
    AdminAccount,
    ApiKey,
    AppSetting,
    ModelPrice,
    UpstreamAttempt,
    UpstreamCredential,
    UsageLog,
    User,
    UserUpstreamRoute,
)
from app.schemas import (
    AdminCreate,
    AdminLoginRequest,
    AdminPasswordChange,
    ModelPriceIn,
    SettingUpdate,
    UserLimitsUpdate,
    UserProvision,
)
from app.token_utils import account_display_name
from app.utils import make_api_key, mask_key, sha256_text, utcnow


@asynccontextmanager
async def lifespan(_: FastAPI):
    await initialize_database()
    async with AsyncSessionLocal() as db:
        days = int(
            await setting(db, "log_retention_days", str(settings.log_retention_days))
        )
        await db.execute(
            delete(UsageLog).where(
                UsageLog.created_at < utcnow() - timedelta(days=days)
            )
        )
        await db.execute(
            delete(UpstreamAttempt).where(
                UpstreamAttempt.created_at < utcnow() - timedelta(days=days)
            )
        )
        await db.commit()
    yield


class UTCJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        def explicit_utc(value, field=""):
            if isinstance(value, dict):
                return {key: explicit_utc(item, key) for key, item in value.items()}
            if isinstance(value, list):
                return [explicit_utc(item, field) for item in value]
            if (
                field.endswith(("_at", "_until"))
                and isinstance(value, str)
                and re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?", value
                )
            ):
                return value + "Z"
            return value

        return super().render(explicit_utc(content))


admin_app = FastAPI(
    title="Looper AI Gateway Admin",
    lifespan=lifespan,
    default_response_class=UTCJSONResponse,
)
STATIC_DIR = Path(__file__).with_name("admin_static")
admin_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="admin-static")


@admin_app.middleware("http")
async def security_headers(request: Request, call_next):
    # Login credentials must never become a query string, even without JS.
    if request.query_params.get("password") or request.query_params.get("p"):
        return Response(status_code=400, headers={"Cache-Control": "no-store"})
    if request.url.path.startswith("/api/"):
        local = (
            request.url.hostname in {"localhost", "127.0.0.1", "::1"}
            and request.client
            and request.client.host in {"127.0.0.1", "::1", "testclient"}
        )
        if settings.admin_require_https and request.url.scheme != "https" and not local:
            return JSONResponse(
                {"detail": "管理后台已要求 HTTPS，请使用 HTTPS 地址访问"},
                status_code=426,
                headers={"Cache-Control": "no-store"},
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse(
                    {"detail": "cross-origin request rejected"}, status_code=403
                )
    response = await call_next(request)
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


@admin_app.get("/", response_class=HTMLResponse)
@admin_app.get("/dashboard", response_class=HTMLResponse)
@admin_app.get("/users", response_class=HTMLResponse)
@admin_app.get("/logs", response_class=HTMLResponse)
@admin_app.get("/settings", response_class=HTMLResponse)
@admin_app.get("/prices", response_class=HTMLResponse)
@admin_app.get("/upstreams", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / "index.html")
        .read_text()
        .replace(
            'data-require-https="true"',
            'data-require-https="' + str(settings.admin_require_https).lower() + '"',
        ),
        headers={"Cache-Control": "no-store"},
    )


async def current_admin(
    request: Request, db: AsyncSession = Depends(get_db)
) -> AdminAccount:
    return await require_admin_session(request, db)


async def superadmin(admin: AdminAccount = Depends(current_admin)) -> AdminAccount:
    if not admin.is_superadmin:
        raise HTTPException(403, "super administrator required")
    return admin


@admin_app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


_login_failures: dict[str, deque] = defaultdict(deque)


@admin_app.post("/api/login")
async def login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    now = monotonic_time.monotonic()
    # Bound memory and expire idle entries. Failed attempts are rate limited
    # per worker; the reverse proxy may impose a shared limit as well.
    for address in list(_login_failures):
        while _login_failures[address] and _login_failures[address][0] < now - 60:
            _login_failures[address].popleft()
        if not _login_failures[address]:
            del _login_failures[address]
    address = request.client.host if request.client else "unknown"
    failures = _login_failures[address]
    if len(failures) >= 5:
        raise HTTPException(
            429, "too many login attempts", headers={"Retry-After": "60"}
        )
    admin = await db.scalar(
        select(AdminAccount).where(AdminAccount.username == payload.username)
    )
    if (
        not admin
        or not admin.enabled
        or not verify_password(payload.password, admin.password_hash)
    ):
        failures.append(now)
        raise HTTPException(401, "invalid username or password")
    _login_failures.pop(address, None)
    token = await create_session(db, admin)
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=7 * 86400,
    )
    return {
        "ok": True,
        "username": admin.username,
        "is_superadmin": admin.is_superadmin,
        "must_change_password": admin.must_change_password,
    }


@admin_app.post("/api/logout")
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    token = request.cookies.get("admin_session")
    if token:
        from app.models import AdminSession

        session = await db.scalar(
            select(AdminSession).where(AdminSession.token_hash == sha256_text(token))
        )
        if session:
            session.revoked_at = utcnow()
            await db.commit()
    response.delete_cookie("admin_session")
    return {"ok": True}


@admin_app.get("/api/me")
async def me(admin: AdminAccount = Depends(current_admin)):
    return {
        "id": admin.id,
        "username": admin.username,
        "is_superadmin": admin.is_superadmin,
        "must_change_password": admin.must_change_password,
    }


@admin_app.post("/api/password")
async def change_password(
    payload: AdminPasswordChange,
    request: Request,
    admin: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    if payload.current_password and not verify_password(
        payload.current_password, admin.password_hash
    ):
        raise HTTPException(400, "current password is incorrect")
    if not payload.current_password and not admin.must_change_password:
        raise HTTPException(400, "current password is required")
    admin.password_hash = hash_password(payload.new_password)
    admin.must_change_password = False
    admin.updated_at = utcnow()
    token = request.cookies.get("admin_session")
    await revoke_admin_sessions(db, admin.id, sha256_text(token) if token else None)
    return {"ok": True, "must_change_password": False}


@admin_app.get("/api/admins")
async def list_admins(
    _: AdminAccount = Depends(superadmin), db: AsyncSession = Depends(get_db)
):
    rows = (await db.scalars(select(AdminAccount).order_by(AdminAccount.id))).all()
    return [
        {
            "id": row.id,
            "username": row.username,
            "is_superadmin": row.is_superadmin,
            "must_change_password": row.must_change_password,
            "enabled": row.enabled,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@admin_app.post("/api/admins")
async def create_admin(
    payload: AdminCreate,
    _: AdminAccount = Depends(superadmin),
    db: AsyncSession = Depends(get_db),
):
    if await db.scalar(
        select(AdminAccount).where(AdminAccount.username == payload.username)
    ):
        raise HTTPException(409, "username already exists")
    password = new_initial_password()
    admin = AdminAccount(
        username=payload.username, password_hash=hash_password(password)
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return {"id": admin.id, "username": admin.username, "initial_password": password}


@admin_app.post("/api/admins/{admin_id}/reset-password")
async def reset_admin_password(
    admin_id: int,
    _: AdminAccount = Depends(superadmin),
    db: AsyncSession = Depends(get_db),
):
    admin = await db.get(AdminAccount, admin_id)
    if not admin or admin.is_superadmin:
        raise HTTPException(404, "administrator not found")
    password = new_initial_password()
    admin.password_hash = hash_password(password)
    admin.must_change_password = True
    admin.updated_at = utcnow()
    await revoke_admin_sessions(db, admin.id)
    return {"ok": True, "username": admin.username, "initial_password": password}


@admin_app.get("/api/users")
async def users(
    _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.scalars(
            select(User).where(User.deleted_at.is_(None)).order_by(User.id)
        )
    ).all()
    result = []
    tz_name = await setting(db, "timezone", settings.timezone or _local_timezone())
    for user in rows:
        keys = (
            await db.scalars(
                select(ApiKey).where(
                    ApiKey.user_id == user.id, ApiKey.deleted_at.is_(None)
                )
            )
        ).all()
        usage = {}
        for period in ("day", "yesterday", "week", "month"):
            start = _period_start(period, tz_name)
            end = _period_start("day", tz_name) if period == "yesterday" else utcnow()
            stats = (
                await db.execute(
                    select(
                        func.coalesce(
                            func.sum(case((UsageLog.is_final.is_(True), 1), else_=0)), 0
                        ),
                        func.coalesce(func.sum(UsageLog.total_tokens), 0),
                        func.coalesce(func.sum(UsageLog.cost_usd), 0),
                    ).where(
                        UsageLog.user_id == user.id,
                        UsageLog.created_at >= start,
                        UsageLog.created_at < end,
                    )
                )
            ).one()
            usage[period] = {
                "requests": stats[0],
                "tokens": stats[1],
                "cost_usd": stats[2],
            }
        result.append(
            {
                "id": user.id,
                "name": user.name,
                "note": user.note,
                "enabled": user.enabled,
                "max_concurrency": user.max_concurrency,
                "max_rpm": user.max_rpm,
                "min_interval_seconds": user.min_interval_seconds,
                "created_at": user.created_at,
                "usage": usage,
                "upstream_ids": list(
                    (
                        await db.scalars(
                            select(UserUpstreamRoute.upstream_credential_id).where(
                                UserUpstreamRoute.user_id == user.id
                            )
                        )
                    ).all()
                ),
                "keys": [
                    {
                        "id": k.id,
                        "name": k.name,
                        "preview": k.key_preview,
                        "enabled": k.enabled,
                    }
                    for k in keys
                ],
            }
        )
    return result


@admin_app.post("/api/users")
async def create_user(
    payload: UserProvision,
    _: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    if await db.scalar(select(User).where(User.name == payload.name)):
        raise HTTPException(409, "username already exists")
    user = User(name=payload.name, note=payload.note, routing_configured=True)
    db.add(user)
    await db.flush()
    raw_key = make_api_key()
    db.add(
        ApiKey(
            user_id=user.id,
            name=payload.key_name,
            key_hash=sha256_text(raw_key),
            key_preview=mask_key(raw_key),
        )
    )
    await db.commit()
    return {"id": user.id, "name": user.name, "api_key": raw_key}


@admin_app.patch("/api/users/{user_id}/limits")
async def update_user_limits(
    user_id: int,
    payload: UserLimitsUpdate,
    _: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(404, "user not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    return {"ok": True}


@admin_app.delete("/api/users/{user_id}")
async def delete_user(
    user_id: int,
    _: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(404, "user not found")
    await db.execute(
        delete(UserUpstreamRoute).where(UserUpstreamRoute.user_id == user_id)
    )
    user.enabled = False
    user.deleted_at = utcnow()
    await db.execute(
        ApiKey.__table__.update()
        .where(ApiKey.user_id == user_id)
        .values(enabled=False, deleted_at=utcnow())
    )
    await db.commit()
    return {"ok": True}


@admin_app.get("/api/prices")
async def prices(
    _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    return (await db.scalars(select(ModelPrice).order_by(ModelPrice.model))).all()


@admin_app.get("/api/prices/unmatched")
async def unmatched_prices(
    _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.scalars(
            select(UsageLog.model)
            .outerjoin(ModelPrice, ModelPrice.model == UsageLog.model)
            .where(UsageLog.model.is_not(None), ModelPrice.id.is_(None))
            .distinct()
            .order_by(UsageLog.model)
        )
    ).all()
    return list(rows)


@admin_app.put("/api/prices")
async def put_price(
    payload: ModelPriceIn,
    _: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    price = await db.scalar(select(ModelPrice).where(ModelPrice.model == payload.model))
    if not price:
        price = ModelPrice(model=payload.model)
        db.add(price)
    for key, value in payload.model_dump().items():
        setattr(price, key, value)
    price.updated_at = utcnow()
    await db.commit()
    return price


async def setting(db: AsyncSession, key: str, default: str) -> str:
    row = await db.get(AppSetting, key)
    return row.value if row else default


@admin_app.get("/api/settings")
async def get_settings(
    _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    return {
        "timezone": await setting(
            db, "timezone", settings.timezone or _local_timezone()
        ),
        "log_retention_days": int(
            await setting(db, "log_retention_days", str(settings.log_retention_days))
        ),
    }


@admin_app.put("/api/settings")
async def update_settings(
    payload: SettingUpdate,
    _: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    changes = payload.model_dump(exclude_none=True)
    if "timezone" in changes:
        try:
            ZoneInfo(changes["timezone"])
        except ZoneInfoNotFoundError:
            raise HTTPException(400, "invalid timezone") from None
    for key, value in changes.items():
        row = await db.get(AppSetting, key)
        if not row:
            row = AppSetting(key=key, value=str(value))
            db.add(row)
        else:
            row.value = str(value)
        row.updated_at = utcnow()
    await db.commit()
    return await get_settings(_, db)


def _local_timezone() -> str:
    return (
        datetime.now().astimezone().tzinfo.key
        if getattr(datetime.now().astimezone().tzinfo, "key", None)
        else "UTC"
    )


def _period_start(name: str, tz_name: str) -> datetime:
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = UTC
    now = datetime.now(zone)
    if name == "day":
        start = now.date()
    elif name == "yesterday":
        start = now.date() - timedelta(days=1)
    elif name == "week":
        start = now.date() - timedelta(days=now.weekday())
    elif name == "month":
        start = now.date().replace(day=1)
    else:
        start = now.date()
    if name == "yesterday":
        start_time = datetime.combine(start, time.min, zone)
    else:
        start_time = datetime.combine(start, time.min, zone)
    return start_time.astimezone(UTC).replace(tzinfo=None)


@admin_app.get("/api/dashboard")
async def dashboard(
    _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    tz_name = await setting(db, "timezone", settings.timezone or _local_timezone())
    totals = {}
    for name in ("day", "yesterday", "week", "month"):
        start = _period_start(name, tz_name)
        end = _period_start("day", tz_name) if name == "yesterday" else utcnow()
        row = (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(case((UsageLog.is_final.is_(True), 1), else_=0)), 0
                    ),
                    func.coalesce(func.sum(UsageLog.total_tokens), 0),
                    func.coalesce(func.sum(UsageLog.cost_usd), 0),
                ).where(UsageLog.created_at >= start, UsageLog.created_at < end)
            )
        ).one()
        totals[name] = {"requests": row[0], "tokens": row[1], "cost_usd": row[2]}
    return {
        "periods": totals,
        "system": _system_metrics(),
        "active_sessions": await db.scalar(
            select(func.count(ActiveSession.id)).where(
                ActiveSession.finished_at.is_(None)
            )
        ),
    }


@admin_app.get("/api/system")
async def system(_: AdminAccount = Depends(current_admin)):
    return _system_metrics()


def _system_metrics() -> dict:
    disk = shutil.disk_usage("/")
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_percent": round(disk.used * 100 / disk.total, 2),
        "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
    }


@admin_app.get("/api/logs")
async def logs(
    limit: int = 100,
    offset: int = 0,
    _: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    limit = min(max(limit, 1), 500)
    rows = (
        await db.execute(
            select(
                UsageLog,
                User.name,
                ApiKey.name,
                UpstreamCredential.name,
                UpstreamCredential.upstream_email,
                UpstreamCredential.upstream_name,
                UpstreamCredential.account_id,
            )
            .join(User, User.id == UsageLog.user_id)
            .join(ApiKey, ApiKey.id == UsageLog.api_key_id)
            .outerjoin(
                UpstreamCredential,
                UpstreamCredential.id == UsageLog.upstream_credential_id,
            )
            .order_by(UsageLog.id.desc())
            .limit(limit)
            .offset(max(offset, 0))
        )
    ).all()
    return [
        {
            "id": log.id,
            "created_at": log.created_at,
            "user": username,
            "key": keyname,
            "model": log.model,
            "path": log.path,
            "gateway_request_id": log.gateway_request_id,
            "display_request_id": display_request_id(
                log.gateway_request_id, log.retry_count
            ),
            "status_code": log.status_code,
            "retry_count": log.retry_count,
            "is_final": log.is_final,
            "upstream_http_status": log.upstream_http_status,
            "failure_category": log.failure_category,
            "elapsed_ms": log.elapsed_ms,
            "input_tokens": log.prompt_tokens,
            "output_tokens": log.completion_tokens,
            "total_tokens": log.total_tokens,
            "cost_usd": log.cost_usd,
            "error": log.error_message,
            "upstream_request_id": log.upstream_request_id,
            "upstream_credential_id": log.upstream_credential_id,
            "upstream_account_id": upstream_account_id,
            "upstream_display_name": account_display_name(
                upstream_name,
                upstream_email,
                upstream_profile_name,
                upstream_account_id,
                log.upstream_credential_id,
            )
            if log.upstream_credential_id is not None
            else None,
        }
        for log, username, keyname, upstream_name, upstream_email, upstream_profile_name, upstream_account_id in rows
    ]


@admin_app.get("/api/logs/{log_id}/attempts")
async def log_attempts(
    log_id: int,
    _: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    log = await db.get(UsageLog, log_id)
    if not log:
        raise HTTPException(404, "request log not found")
    if not log.gateway_request_id:
        return []
    return (
        await db.scalars(
            select(UpstreamAttempt)
            .where(UpstreamAttempt.gateway_request_id == log.gateway_request_id)
            .order_by(UpstreamAttempt.attempt)
        )
    ).all()


@admin_app.delete("/api/logs")
async def clear_logs(
    all_logs: bool = False,
    _: AdminAccount = Depends(superadmin),
    db: AsyncSession = Depends(get_db),
):
    if all_logs:
        result = await db.execute(delete(UsageLog))
        await db.execute(delete(UpstreamAttempt))
    else:
        days = int(
            await setting(db, "log_retention_days", str(settings.log_retention_days))
        )
        await db.execute(
            delete(UpstreamAttempt).where(
                UpstreamAttempt.created_at < utcnow() - timedelta(days=days)
            )
        )
        result = await db.execute(
            delete(UsageLog).where(
                UsageLog.created_at < utcnow() - timedelta(days=days)
            )
        )
    await db.commit()
    return {"deleted": result.rowcount or 0}


@admin_app.get("/api/sessions")
async def sessions(
    recent_seconds: int = Query(default=0, ge=0, le=3600),
    _: AdminAccount = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    stale_before = utcnow() - timedelta(hours=2)
    stale = (
        await db.scalars(
            select(ActiveSession).where(
                ActiveSession.finished_at.is_(None),
                ActiveSession.last_event_at < stale_before,
            )
        )
    ).all()
    for session in stale:
        session.status = "stale"
        session.finished_at = utcnow()
    if stale:
        await db.commit()
    visible = ActiveSession.finished_at.is_(None)
    if recent_seconds:
        visible = or_(
            visible,
            ActiveSession.finished_at >= utcnow() - timedelta(seconds=recent_seconds),
        )
    rows = (
        await db.execute(
            select(ActiveSession, User.name)
            .join(User, User.id == ActiveSession.user_id)
            .where(visible)
            .order_by(
                ActiveSession.finished_at.is_not(None), ActiveSession.started_at.desc()
            )
        )
    ).all()
    return [
        {
            "id": row.id,
            "user": name,
            "model": row.model,
            "path": row.path,
            "status": row.status,
            "started_at": row.started_at,
            "last_event_at": row.last_event_at,
            "finished_at": row.finished_at,
        }
        for row, name in rows
    ]


admin_app.include_router(upstream_router, dependencies=[Depends(current_admin)])
