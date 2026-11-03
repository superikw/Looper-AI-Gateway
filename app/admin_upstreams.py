"""Session-authenticated upstream maintenance and bidirectional routing."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.http_client import get_http_client
from app.locks import database_lock
from app.models import (
    AppSetting,
    UpstreamRequest,
    User,
    UserUpstreamRoute,
)
from app.oauth_manual import (
    complete_oauth_session,
    get_active_upstream_credential,
    logout_upstream_credentials,
    start_oauth_session,
)
from app.oauth_schemas import OAuthCompleteRequest
from app.schemas import UpstreamAccountUpdate, UpstreamRouteUpdate, UserRouteUpdate
from app.upstreams import list_accounts, update_account, validate_binding
from app.utils import utcnow

router = APIRouter(prefix="/api")


@router.get("/upstreams")
async def accounts(db: AsyncSession = Depends(get_db)):
    rows = await list_accounts(db)
    routes = (await db.execute(select(UserUpstreamRoute))).scalars().all()
    active = dict(
        (
            await db.execute(
                select(UpstreamRequest.upstream_credential_id, func.count())
                .where(UpstreamRequest.active.is_(True))
                .group_by(UpstreamRequest.upstream_credential_id)
            )
        ).all()
    )
    for row in rows:
        cid = row["id"]
        row["user_ids"] = [r.user_id for r in routes if r.upstream_credential_id == cid]
        row["active_sessions"] = active.get(cid, 0)
        snapshot = await db.get(AppSetting, f"upstream_usage:{cid}")
        row["quota"] = json.loads(snapshot.value) if snapshot else None
        row["status"] = (
            "disabled"
            if not row["enabled"]
            else "cooldown"
            if row["cooldown_until"] and row["cooldown_until"] > utcnow()
            else "expired"
            if row["expires_at"] and row["expires_at"] <= utcnow()
            else "ready"
        )
    return {"auth_mode": settings.upstream_auth_mode, "accounts": rows}


@router.post("/upstreams/oauth/start")
async def oauth_start(db: AsyncSession = Depends(get_db)):
    return await start_oauth_session(db)


@router.post("/upstreams/oauth/complete")
async def oauth_complete(
    payload: OAuthCompleteRequest, db: AsyncSession = Depends(get_db)
):
    result = await complete_oauth_session(
        db, payload.session_id, payload.callback_url, payload.name
    )
    # Credential exchange response contains metadata only in the shared service.
    return {"ok": True, "credential_id": result["credential_id"]}


@router.patch("/upstreams/{cid}")
async def patch_account(
    cid: int, payload: UpstreamAccountUpdate, db: AsyncSession = Depends(get_db)
):
    changes = payload.model_dump(exclude_unset=True)
    if any(value is None for key, value in changes.items() if key != "name"):
        raise HTTPException(400, "account fields cannot be null")
    await update_account(db, cid, changes)
    return {"ok": True}


@router.delete("/upstreams/{cid}")
async def remove_account(cid: int, db: AsyncSession = Depends(get_db)):
    if not await logout_upstream_credentials(db, cid):
        raise HTTPException(404, "upstream account not found")
    await db.execute(
        delete(UserUpstreamRoute).where(UserUpstreamRoute.upstream_credential_id == cid)
    )
    await db.commit()
    return {"ok": True}


@router.put("/users/{uid}/upstreams")
async def user_routes(
    uid: int, payload: UserRouteUpdate, db: AsyncSession = Depends(get_db)
):
    async with database_lock("user-upstream-routes"):
        user = await db.get(User, uid)
        if not user or user.deleted_at is not None:
            raise HTTPException(404, "user not found")
        user.routing_configured = True
        ids = set(payload.upstream_ids)
        for cid in ids:
            await validate_binding(db, cid)
        await db.execute(
            delete(UserUpstreamRoute).where(UserUpstreamRoute.user_id == uid)
        )
        db.add_all(
            UserUpstreamRoute(user_id=uid, upstream_credential_id=cid) for cid in ids
        )
        await db.commit()
    return {"ok": True, "upstream_ids": sorted(ids)}


@router.put("/upstreams/{cid}/users")
async def account_routes(
    cid: int, payload: UpstreamRouteUpdate, db: AsyncSession = Depends(get_db)
):
    async with database_lock("user-upstream-routes"):
        await validate_binding(db, cid)
        ids = set(payload.user_ids)
        found = set(
            (
                await db.scalars(
                    select(User.id).where(User.id.in_(ids), User.deleted_at.is_(None))
                )
            ).all()
        )
        if found != ids:
            raise HTTPException(404, "user not found")
        # Users removed from this account must retain an explicit empty policy.
        old_ids = set(
            (
                await db.scalars(
                    select(UserUpstreamRoute.user_id).where(
                        UserUpstreamRoute.upstream_credential_id == cid
                    )
                )
            ).all()
        )
        for uid in ids | old_ids:
            user = await db.get(User, uid)
            user.routing_configured = True
        await db.execute(
            delete(UserUpstreamRoute).where(
                UserUpstreamRoute.upstream_credential_id == cid
            )
        )
        db.add_all(
            UserUpstreamRoute(user_id=uid, upstream_credential_id=cid) for uid in ids
        )
        await db.commit()
    return {"ok": True, "user_ids": sorted(ids)}


def quota_window(window):
    if not isinstance(window, dict):
        return None
    reset = window.get("reset_at")
    try:
        reset_at = (
            datetime.fromtimestamp(float(reset), UTC).isoformat()
            if reset is not None
            else None
        )
        used = float(window["used_percent"])
        if not 0 <= used <= 100:
            return None
        return {
            "used_percent": used,
            "reset_at": reset_at,
            "window_seconds": window.get("limit_window_seconds"),
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


@router.post("/upstreams/{cid}/usage/refresh")
async def refresh_usage(cid: int, db: AsyncSession = Depends(get_db)):
    await validate_binding(db, cid)
    credential = await get_active_upstream_credential(db, cid)
    if not credential:
        raise HTTPException(409, "enable the account before refreshing usage")
    try:
        response = await get_http_client().get(
            settings.upstream_base_url.rstrip("/") + "/wham/usage",
            headers={
                "Authorization": f"Bearer {credential.access_token}",
                "ChatGPT-Account-Id": credential.account_id or "",
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        limits = data.get("rate_limit") or {}
        quota = {
            "checked_at": utcnow().replace(tzinfo=UTC).isoformat(),
            "plan_type": data.get("plan_type"),
            "primary": quota_window(limits.get("primary_window")),
            "secondary": quota_window(limits.get("secondary_window")),
        }
    except (httpx.HTTPError, ValueError, AttributeError):
        raise HTTPException(
            502, "upstream quota unavailable; last successful snapshot retained"
        ) from None
    key = f"upstream_usage:{cid}"
    snapshot = await db.get(AppSetting, key)
    if snapshot:
        snapshot.value = json.dumps(quota)
        snapshot.updated_at = utcnow()
    else:
        db.add(AppSetting(key=key, value=json.dumps(quota)))
    await db.commit()
    return quota
