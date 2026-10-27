"""Persistent account-pool policy and administration for the SQLite gateway."""

from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import case, or_, select, update

from app.config import settings
from app.db import AsyncSessionLocal
from app.locks import database_lock
from app.models import UpstreamCredential
from app.oauth_manual import get_active_upstream_credential
from app.token_utils import extract_account_id_from_token
from app.utils import utcnow

ACCOUNT_FIELDS = (
    "id",
    "name",
    "account_id",
    "enabled",
    "deleted_at",
    "expires_at",
    "cooldown_until",
    "last_used_at",
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


async def claim_account(bound_id: int | None, excluded: set[int]) -> int | None:
    # The short process lock makes least-recently-used selection consistent
    # across workers. It is never held while contacting the upstream.
    async with database_lock("account-pool"), AsyncSessionLocal() as db:
        query = available_account_ids()
        if bound_id is not None:
            query = query.where(UpstreamCredential.id == bound_id)
        if excluded:
            query = query.where(UpstreamCredential.id.not_in(excluded))
        query = query.order_by(
            UpstreamCredential.last_used_at.asc().nulls_first(),
            UpstreamCredential.id.asc(),
        ).limit(1)
        cid = await db.scalar(query)
        if cid is not None:
            await db.execute(
                update(UpstreamCredential)
                .where(UpstreamCredential.id == cid)
                .values(
                    last_used_at=utcnow(),
                    request_count=UpstreamCredential.request_count + 1,
                )
            )
            await db.commit()
        return cid


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
    async with (
        database_lock("oauth-refresh", settings.upstream_timeout_seconds + 5),
        database_lock(f"oauth-refresh-{cid}", settings.upstream_timeout_seconds + 5),
    ):
        await validate_binding(db, cid)
        values = {
            key: value for key, value in changes.items() if key in ("name", "enabled")
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
