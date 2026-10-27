from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import ApiKey
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
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing api key"
        )

    raw_key = authorization.removeprefix("Bearer ").strip()
    hashed = sha256_text(raw_key)
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == hashed))
    api_key = result.scalar_one_or_none()
    if not api_key or not api_key.enabled or api_key.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or disabled api key",
        )
    return api_key
