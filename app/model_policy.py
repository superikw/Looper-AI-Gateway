"""Database-backed model blacklist, shared by the API and local CLI."""

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.model_utils import canonicalize_model_name
from app.models import BlockedModel


def validate_model(model: str | None) -> str:
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, "model must be a non-empty string")
    normalized = canonicalize_model_name(model)
    if len(normalized) > 128:
        raise HTTPException(400, "model must contain at most 128 characters")
    return normalized


async def enforce_model_policy(db: AsyncSession, model: str) -> None:
    # Query each request so CLI/admin changes are visible across workers.
    if await db.scalar(select(BlockedModel.model).where(BlockedModel.model == model)):
        raise HTTPException(403, f"model blocked: {model}")


async def list_blocked_models(db: AsyncSession) -> list[BlockedModel]:
    return list(
        (await db.scalars(select(BlockedModel).order_by(BlockedModel.model))).all()
    )


async def block_model(db: AsyncSession, model: str) -> BlockedModel:
    model = validate_model(model)
    await db.execute(insert(BlockedModel).values(model=model).on_conflict_do_nothing())
    item = (
        await db.scalars(select(BlockedModel).where(BlockedModel.model == model))
    ).one()
    await db.commit()
    return item


async def unblock_model(db: AsyncSession, model: str) -> str:
    model = validate_model(model)
    await db.execute(delete(BlockedModel).where(BlockedModel.model == model))
    await db.commit()
    return model
