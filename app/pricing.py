"""Bounded, cached price discovery. Only exact model/provider IDs are matched."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.config import settings
from app.models import ModelPrice

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
OFFICIAL_URLS = {
    "openai": "https://developers.openai.com/api/docs/pricing",
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
}
logger = logging.getLogger(__name__)
_catalogs: tuple[dict, list] = ({}, [])
_cache_until = 0.0
_lock = asyncio.Lock()


async def catalogs() -> tuple[dict, list]:
    global _catalogs, _cache_until
    if time.monotonic() < _cache_until:
        return _catalogs
    async with _lock:
        if time.monotonic() < _cache_until:
            return _catalogs
        async with httpx.AsyncClient(
            proxy=settings.upstream_proxy_url or None, timeout=3
        ) as client:

            async def fetch(url):
                try:
                    async with asyncio.timeout(3):
                        response = await client.get(url)
                        response.raise_for_status()
                        return response.json()
                except (TimeoutError, httpx.HTTPError, ValueError):
                    logger.warning("Price catalog unavailable: %s", url)
                    return None

            lite, router = await asyncio.gather(
                fetch(LITELLM_URL), fetch(OPENROUTER_URL)
            )
        old_lite, old_router = _catalogs
        valid_lite = isinstance(lite, dict) and bool(lite)
        valid_router = isinstance(router, dict) and isinstance(router.get("data"), list)
        _catalogs = (
            lite if valid_lite else old_lite,
            router["data"] if valid_router else old_router,
        )
        _cache_until = time.monotonic() + (3600 if valid_lite and valid_router else 60)
        return _catalogs


def per_million(value) -> float:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("invalid price")
    result = float(number * 1_000_000)
    if result == float("inf"):
        raise ValueError("invalid price")
    return result


def match_price(model: str, lite: dict, router: list) -> dict | None:
    provider, _, bare = model.partition("/")
    if not bare:
        bare, provider = model, ""
    expected = provider or (
        "openai"
        if bare.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))
        else "anthropic"
        if bare.startswith("claude-")
        else ""
    )
    candidates = []
    for name in dict.fromkeys((model, bare, f"{expected}/{bare}")):
        entry = lite.get(name)
        if not isinstance(entry, dict):
            continue
        actual = entry.get("litellm_provider", "")
        # Never substitute another vendor's deployment with a similar name.
        if expected and actual != expected:
            continue
        candidates.append((actual not in OFFICIAL_URLS, name, entry))
    for _, name, entry in sorted(candidates, key=lambda item: item[0]):
        try:
            inp = per_million(entry["input_cost_per_token"])
            return dict(
                input_usd_per_million=inp,
                output_usd_per_million=per_million(entry["output_cost_per_token"]),
                cached_input_usd_per_million=per_million(
                    entry.get(
                        "cache_read_input_token_cost", entry["input_cost_per_token"]
                    )
                ),
                source=f"litellm:{name}",
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    for entry in router:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id", "")
        if identifier != model and identifier != f"{expected}/{bare}":
            continue
        try:
            price = entry["pricing"]
            return dict(
                input_usd_per_million=per_million(price["prompt"]),
                output_usd_per_million=per_million(price["completion"]),
                cached_input_usd_per_million=per_million(
                    price.get("input_cache_read", price["prompt"])
                ),
                source=f"openrouter:{identifier}",
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return None


async def ensure_model_price(db, model: str | None) -> ModelPrice | None:
    if not model:
        return None
    existing = await db.scalar(select(ModelPrice).where(ModelPrice.model == model))
    if existing:
        return existing
    price = match_price(model, *(await catalogs()))
    if price is None:
        return None
    # Parallel first requests and manual edits must not overwrite one another.
    await db.execute(
        insert(ModelPrice)
        .values(model=model, **price)
        .on_conflict_do_nothing(index_elements=["model"])
    )
    return await db.scalar(select(ModelPrice).where(ModelPrice.model == model))
