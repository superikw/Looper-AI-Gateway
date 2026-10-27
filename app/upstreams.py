"""Persistent account-pool policy and administration for the SQLite gateway."""

import asyncio
import math
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

import anyio
from fastapi import HTTPException
from sqlalchemy import case, delete, or_, select, update

from app.config import settings
from app.db import AsyncSessionLocal
from app.locks import database_lock, lock_path, try_process_lock
from app.models import UpstreamCredential, UpstreamRequest
from app.oauth_manual import get_active_upstream_credential
from app.token_utils import extract_account_id_from_token
from app.utils import utcnow

ACCOUNT_FIELDS = (
    "id",
    "name",
    "account_id",
    "enabled",
    "max_rpm",
    "max_concurrency",
    "min_interval_seconds",
    "deleted_at",
    "expires_at",
    "cooldown_until",
    "last_used_at",
    "last_dispatched_at",
    "request_count",
    "failure_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "created_at",
    "updated_at",
)


def available_account_ids():
    return select(UpstreamCredential.id).where(
        UpstreamCredential.provider == "codex",
        UpstreamCredential.deleted_at.is_(None),
        UpstreamCredential.enabled.is_(True),
        or_(
            UpstreamCredential.cooldown_until.is_(None),
            UpstreamCredential.cooldown_until <= utcnow(),
        ),
    )


@dataclass
class AccountLease:
    id: str
    credential_id: int
    fd: int | None

    async def start_request(self, deadline: float) -> None:
        # Keep pending refreshes reserved even if they take longer than a minute.
        # Move the timestamp to the real dispatch time, so slow authentication
        # cannot bunch multiple admission windows into one upstream burst.
        while True:
            async with database_lock("account-pool"), AsyncSessionLocal() as db:
                now = utcnow()
                account = (
                    await db.execute(
                        select(
                            UpstreamCredential.min_interval_seconds,
                            UpstreamCredential.last_dispatched_at,
                        ).where(UpstreamCredential.id == self.credential_id)
                    )
                ).one()
                last = account.last_dispatched_at
                delay = (
                    (
                        last + timedelta(seconds=account.min_interval_seconds) - now
                    ).total_seconds()
                    if last and account.min_interval_seconds
                    else 0
                )
                if delay <= 0:
                    await db.execute(
                        update(UpstreamRequest)
                        .where(UpstreamRequest.id == self.id)
                        .values(started_at=now, dispatched=True)
                    )
                    await db.execute(
                        update(UpstreamCredential)
                        .where(UpstreamCredential.id == self.credential_id)
                        .values(last_dispatched_at=now)
                    )
                    await db.commit()
                    return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise HTTPException(
                    429,
                    "upstream account minimum request interval exceeded",
                    headers={"Retry-After": str(math.ceil(delay))},
                )
            await asyncio.sleep(min(0.1, remaining))

    async def release(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        # Keep the OS lock until the database release completes. If the worker
        # dies, the OS releases it and the next scheduler recovers the slot.
        with anyio.CancelScope(shield=True):
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(UpstreamRequest)
                        .where(UpstreamRequest.id == self.id)
                        .values(active=False)
                    )
                    await db.commit()
            finally:
                os.close(fd)


async def claim_account(
    bound_id: int | None, excluded: set[int]
) -> AccountLease | None:
    # Selection, rolling-window accounting and reservations share one short
    # process lock. Each in-flight request also holds its own OS lock, so long
    # streams need no lease timeout and crashed workers cannot strand slots.
    lease = None
    try:
        async with database_lock("account-pool"), AsyncSessionLocal() as db:
            now = utcnow()
            cutoff = now - timedelta(seconds=60)
            requests = list(
                (
                    await db.scalars(
                        select(UpstreamRequest).where(
                            or_(
                                UpstreamRequest.started_at > cutoff,
                                UpstreamRequest.active.is_(True),
                            )
                        )
                    )
                ).all()
            )
            recent_by_account = defaultdict(list)
            active_by_account = defaultdict(int)
            for request in requests:
                if request.active:
                    fd = try_process_lock(f"account-request-{request.id}")
                    if fd is not None:
                        os.close(fd)
                        request.active = False
                active_by_account[request.upstream_credential_id] += request.active
                if request.active and not request.dispatched:
                    recent_by_account[request.upstream_credential_id].append(now)
                elif request.started_at > cutoff:
                    recent_by_account[request.upstream_credential_id].append(
                        request.started_at
                    )
            await db.flush()
            expired = list(
                (
                    await db.scalars(
                        delete(UpstreamRequest)
                        .where(
                            UpstreamRequest.started_at <= cutoff,
                            UpstreamRequest.active.is_(False),
                        )
                        .returning(UpstreamRequest.id)
                    )
                ).all()
            )
            query = select(
                UpstreamCredential.id,
                UpstreamCredential.max_rpm,
                UpstreamCredential.max_concurrency,
                UpstreamCredential.min_interval_seconds,
                UpstreamCredential.last_used_at,
                UpstreamCredential.last_dispatched_at,
            ).where(UpstreamCredential.id.in_(available_account_ids()))
            if bound_id is not None:
                query = query.where(UpstreamCredential.id == bound_id)
            if excluded:
                query = query.where(UpstreamCredential.id.not_in(excluded))
            candidates = (
                await db.execute(
                    query.order_by(
                        UpstreamCredential.last_used_at.asc().nulls_first(),
                        UpstreamCredential.id.asc(),
                    )
                )
            ).all()
            selected = None
            retry_after = None
            for account in candidates:
                recent = sorted(recent_by_account[account.id])
                active = active_by_account[account.id]
                delay = 0
                if len(recent) >= account.max_rpm:
                    delay = max(
                        delay,
                        (
                            recent[-account.max_rpm] + timedelta(seconds=60) - now
                        ).total_seconds(),
                    )
                if active >= account.max_concurrency:
                    delay = max(delay, 1)
                last_start = max(
                    filter(None, (account.last_used_at, account.last_dispatched_at)),
                    default=None,
                )
                if last_start and account.min_interval_seconds:
                    delay = max(
                        delay,
                        (
                            last_start
                            + timedelta(seconds=account.min_interval_seconds)
                            - now
                        ).total_seconds(),
                    )
                if delay > 0:
                    retry_after = (
                        min(retry_after, math.ceil(delay))
                        if retry_after is not None
                        else math.ceil(delay)
                    )
                    continue
                selected = account.id
                break
            if selected is not None:
                rid = uuid.uuid4().hex
                fd = try_process_lock(f"account-request-{rid}")
                if fd is None:
                    raise HTTPException(503, "could not reserve upstream account")
                lease = AccountLease(rid, selected, fd)
                db.add(
                    UpstreamRequest(
                        id=rid,
                        upstream_credential_id=selected,
                        started_at=now,
                        active=True,
                    )
                )
                await db.execute(
                    update(UpstreamCredential)
                    .where(UpstreamCredential.id == selected)
                    .values(
                        last_used_at=now,
                        request_count=UpstreamCredential.request_count + 1,
                    )
                )
            await db.commit()
            for rid in expired:
                lock_path(f"account-request-{rid}").unlink(missing_ok=True)
            if lease is None and candidates:
                raise HTTPException(
                    429,
                    "upstream account request capacity exceeded",
                    headers={"Retry-After": str(retry_after or 1)},
                )
            return lease

    except BaseException:
        if lease is not None and lease.fd is not None:
            os.close(lease.fd)
            lease.fd = None
        raise


async def wait_for_account(
    bound_id: int | None, excluded: set[int], deadline: float
) -> AccountLease | None:
    while True:
        try:
            return await claim_account(bound_id, excluded)
        except HTTPException as exc:
            if exc.status_code != 429:
                raise
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(0.1, remaining))


async def mark_account_failure(cid: int | None, cooldown_seconds: int) -> None:
    if cid is None:
        return
    until = utcnow() + timedelta(seconds=cooldown_seconds)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(UpstreamCredential)
            .where(UpstreamCredential.id == cid)
            .values(
                failure_count=UpstreamCredential.failure_count + 1,
                cooldown_until=case(
                    (
                        or_(
                            UpstreamCredential.cooldown_until.is_(None),
                            UpstreamCredential.cooldown_until < until,
                        ),
                        until,
                    ),
                    else_=UpstreamCredential.cooldown_until,
                ),
            )
        )
        await db.commit()


async def pool_is_ready(db) -> bool:
    # Probe every eligible account without advancing the scheduling cursor.
    ids = list((await db.scalars(available_account_ids())).all())
    for cid in ids:
        try:
            credential = await get_active_upstream_credential(db, cid)
            if credential and (
                credential.account_id
                or extract_account_id_from_token(credential.access_token)
            ):
                return True
        except HTTPException:
            continue
    return False


async def validate_binding(db, cid: int | None) -> None:
    if cid is None:
        return
    exists = await db.scalar(
        select(UpstreamCredential.id).where(
            UpstreamCredential.id == cid,
            UpstreamCredential.provider == "codex",
            UpstreamCredential.deleted_at.is_(None),
        )
    )
    if exists is None:
        raise HTTPException(404, "upstream account not found")


async def list_accounts(db, include_deleted: bool = False) -> list[dict]:
    # Select metadata only: listing the pool does not decrypt any token.
    query = (
        select(*(getattr(UpstreamCredential, field) for field in ACCOUNT_FIELDS))
        .where(UpstreamCredential.provider == "codex")
        .order_by(UpstreamCredential.id)
    )
    if not include_deleted:
        query = query.where(UpstreamCredential.deleted_at.is_(None))
    return [dict(row._mapping) for row in (await db.execute(query)).all()]


async def update_account(db, cid: int, changes: dict) -> None:
    try:
        async with (
            database_lock("oauth-refresh", settings.upstream_timeout_seconds + 5),
            database_lock(
                f"oauth-refresh-{cid}", settings.upstream_timeout_seconds + 5
            ),
            database_lock("account-pool"),
        ):
            await validate_binding(db, cid)
            values = {
                key: value
                for key, value in changes.items()
                if key
                in (
                    "name",
                    "enabled",
                    "max_rpm",
                    "max_concurrency",
                    "min_interval_seconds",
                )
            }
            if changes.get("reset_cooldown"):
                values["cooldown_until"] = None
            if values:
                values["updated_at"] = utcnow()
                await db.execute(
                    update(UpstreamCredential)
                    .where(UpstreamCredential.id == cid)
                    .values(**values)
                )
                await db.commit()
    except TimeoutError:
        raise HTTPException(
            504, "timed out waiting to update upstream account"
        ) from None
