from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAccount, AdminSession
from app.utils import sha256_text, utcnow

PASSWORD_ROUNDS = 310_000
SESSION_DAYS = 7


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ROUNDS)
    return f"pbkdf2_sha256${PASSWORD_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(rounds)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (TypeError, ValueError):
        return False


def new_initial_password() -> str:
    return secrets.token_urlsafe(15)


async def create_session(db: AsyncSession, admin: AdminAccount) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        AdminSession(
            admin_id=admin.id,
            token_hash=sha256_text(token),
            expires_at=utcnow() + timedelta(days=SESSION_DAYS),
        )
    )
    await db.commit()
    return token


async def require_admin_session(request: Request, db: AsyncSession) -> AdminAccount:
    token = request.cookies.get("admin_session")
    if not token and request.headers.get("authorization", "").startswith("Bearer "):
        token = request.headers["authorization"].removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "admin login required")
    session = await db.scalar(
        select(AdminSession).where(
            AdminSession.token_hash == sha256_text(token),
            AdminSession.revoked_at.is_(None),
            AdminSession.expires_at > utcnow(),
        )
    )
    if not session:
        raise HTTPException(401, "admin session expired")
    admin = await db.get(AdminAccount, session.admin_id)
    if not admin or not admin.enabled:
        raise HTTPException(401, "admin account disabled")
    if admin.must_change_password and request.url.path not in {
        "/api/me",
        "/api/password",
        "/api/logout",
    }:
        raise HTTPException(403, "password change required")
    session.last_seen_at = utcnow()
    await db.commit()
    return admin


async def revoke_admin_sessions(
    db: AsyncSession, admin_id: int, keep_token_hash: str | None = None
) -> None:
    sessions = (
        await db.scalars(
            select(AdminSession).where(
                AdminSession.admin_id == admin_id,
                AdminSession.revoked_at.is_(None),
            )
        )
    ).all()
    now = utcnow()
    for session in sessions:
        if session.token_hash != keep_token_hash:
            session.revoked_at = now
    await db.commit()
