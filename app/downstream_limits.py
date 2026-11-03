"""Per-user admission shared by API keys and worker processes."""

import math
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta

import anyio
from fastapi import HTTPException
from sqlalchemy import delete, or_, select

from app.db import AsyncSessionLocal
from app.locks import database_lock, lock_path, try_process_lock
from app.models import User, UserRequest
from app.utils import utcnow


@dataclass
class UserLease:
    id: str
    fd: int | None

    async def release(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        with anyio.CancelScope(shield=True):
            try:
                async with AsyncSessionLocal() as db:
                    row = await db.get(UserRequest, self.id)
                    if row:
                        row.active = False
                        await db.commit()
            finally:
                os.close(fd)


async def claim_user(user_id: int) -> UserLease:
    lease = None
    try:
        async with database_lock("downstream-limits"), AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            if not user or not user.enabled or user.deleted_at is not None:
                raise HTTPException(401, "user is disabled")
            now = utcnow()
            cutoff = now - timedelta(seconds=max(60, user.min_interval_seconds))
            rows = (
                await db.scalars(
                    select(UserRequest).where(
                        UserRequest.user_id == user_id,
                        or_(
                            UserRequest.started_at > cutoff,
                            UserRequest.active.is_(True),
                        ),
                    )
                )
            ).all()
            for row in rows:
                if row.active:
                    fd = try_process_lock(f"user-request-{row.id}")
                    if fd is not None:
                        os.close(fd)
                        row.active = False
            await db.flush()
            expired = list(
                (
                    await db.scalars(
                        delete(UserRequest)
                        .where(
                            UserRequest.user_id == user_id,
                            UserRequest.started_at <= cutoff,
                            UserRequest.active.is_(False),
                        )
                        .returning(UserRequest.id)
                    )
                ).all()
            )
            recent = sorted(
                row.started_at
                for row in rows
                if row.started_at > now - timedelta(seconds=60)
            )
            delay = 0
            if sum(row.active for row in rows) >= user.max_concurrency:
                delay = 1
            if user.max_rpm and len(recent) >= user.max_rpm:
                delay = max(
                    delay,
                    (
                        recent[-user.max_rpm] + timedelta(seconds=60) - now
                    ).total_seconds(),
                )
            last = max((row.started_at for row in rows), default=None)
            if last:
                delay = max(
                    delay,
                    (
                        last + timedelta(seconds=user.min_interval_seconds) - now
                    ).total_seconds(),
                )
            if delay > 0:
                await db.commit()
                for rid in expired:
                    lock_path(f"user-request-{rid}").unlink(missing_ok=True)
                raise HTTPException(
                    429,
                    "downstream user request limit exceeded",
                    headers={"Retry-After": str(max(1, math.ceil(delay)))},
                )
            rid = uuid.uuid4().hex
            fd = try_process_lock(f"user-request-{rid}")
            if fd is None:
                raise HTTPException(503, "could not reserve user capacity")
            lease = UserLease(rid, fd)
            db.add(UserRequest(id=rid, user_id=user_id, started_at=now, active=True))
            await db.commit()
            for rid in expired:
                lock_path(f"user-request-{rid}").unlink(missing_ok=True)
            return lease
    except BaseException:
        if lease and lease.fd is not None:
            os.close(lease.fd)
            lease.fd = None
        raise


class DownstreamLimitsMiddleware:
    """Hold admission through the last streamed byte and cancellation cleanup."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        finally:
            lease = scope.pop("downstream_lease", None)
            if lease:
                await lease.release()
