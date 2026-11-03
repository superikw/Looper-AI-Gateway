from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import ActiveSession
from app.utils import utcnow


async def start_active_session(db: AsyncSession, user_id: int, api_key_id: int, path: str, model: str) -> str:
    session_id = "sess_" + uuid.uuid4().hex
    db.add(ActiveSession(id=session_id, user_id=user_id, api_key_id=api_key_id, path=path, model=model))
    await db.commit()
    return session_id


async def finish_active_session(session_id: str, status: str) -> None:
    async with AsyncSessionLocal() as db:
        entity = await db.scalar(select(ActiveSession).where(ActiveSession.id == session_id))
        if entity:
            entity.status = status
            entity.finished_at = utcnow()
            entity.last_event_at = utcnow()
            await db.commit()
