from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import ApiKey, User
from app.utils import sha256_text


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    if not settings.admin_token_is_secure:
        raise HTTPException(
            status_code=503,
            detail="administrator authentication is not configured securely",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing admin token"
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), settings.admin_token.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin token"
        )


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing api key"
        )

    raw_key = authorization.removeprefix("Bearer ").strip()
    hashed = sha256_text(raw_key)
    result = await db.execute(
        select(ApiKey)
        .join(User, User.id == ApiKey.user_id)
        .where(
            ApiKey.key_hash == hashed,
            User.enabled.is_(True),
            User.deleted_at.is_(None),
        )
    )
    api_key = result.scalar_one_or_none()
    if not api_key or not api_key.enabled or api_key.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or disabled api key",
        )
    user = await db.get(User, api_key.user_id)
    # Old integrations may provision database rows directly. Public creation
    # APIs and the upgrade migration opt users into explicit route assignment.
    if user.routing_configured:
        db.info["route_user_id"] = api_key.user_id
    from app.downstream_limits import claim_user

    try:
        request.scope["downstream_lease"] = await claim_user(api_key.user_id)
    except HTTPException as exc:
        if exc.status_code == 429:
            from app.services import record_usage

            await record_usage(
                db,
                api_key,
                request.url.path,
                None,
                429,
                {"error": {"message": str(exc.detail)}},
                None,
            )
        raise
    return api_key
