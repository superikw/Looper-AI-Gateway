import asyncio

import pytest
from sqlalchemy import func, select

from app.db import AsyncSessionLocal
from app.models import ModelPrice, UsageLog
from app.pricing import catalogs as load_catalogs
from app.pricing import ensure_model_price, match_price
from app.services import record_usage

LITE = {
    "gpt-example": {
        "litellm_provider": "openai",
        "input_cost_per_token": 0.000002,
        "output_cost_per_token": 0.000008,
        "cache_read_input_token_cost": 0.0000005,
    },
    "azure/gpt-example": {
        "litellm_provider": "azure",
        "input_cost_per_token": 0.000001,
        "output_cost_per_token": 0.000002,
    },
}
ROUTER = [
    {
        "id": "openai/gpt-example",
        "pricing": {"prompt": "0.000003", "completion": "0.000009"},
    }
]


def test_exact_official_provider_priority_and_no_fuzzy_match():
    price = match_price("openai/gpt-example", LITE, ROUTER)
    assert price["input_usd_per_million"] == 2
    assert price["cached_input_usd_per_million"] == 0.5
    assert price["source"] == "litellm:gpt-example"
    assert match_price("gpt-example-unknown", LITE, ROUTER) is None
    assert (
        match_price(
            "openai/gpt-example", {"azure/gpt-example": LITE["azure/gpt-example"]}, []
        )
        is None
    )
    fallback = match_price("gpt-example", {}, ROUTER)
    assert fallback["input_usd_per_million"] == 3
    assert fallback["cached_input_usd_per_million"] == 3


@pytest.mark.parametrize("bad", [-1, "NaN", "Infinity", None, "bad"])
def test_invalid_catalog_values_are_rejected(bad):
    lite = {"gpt-example": dict(LITE["gpt-example"], input_cost_per_token=bad)}
    assert match_price("gpt-example", lite, []) is None


async def test_auto_price_first_request_and_cache_cost(key, monkeypatch):
    async def catalogs():
        return LITE, ROUTER

    monkeypatch.setattr("app.pricing.catalogs", catalogs)
    async with AsyncSessionLocal() as db:
        await record_usage(
            db,
            key,
            "/v1/responses",
            "gpt-example",
            200,
            {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 40},
                }
            },
            None,
        )
    async with AsyncSessionLocal() as db:
        price = await db.scalar(select(ModelPrice))
        log = await db.scalar(select(UsageLog))
        assert price.model == "gpt-example"
        assert log.cost_usd == pytest.approx((60 * 2 + 40 * 0.5 + 20 * 8) / 1_000_000)


async def test_manual_price_never_fetches_or_overwrites(key, monkeypatch):
    async def fail():
        raise AssertionError("manual price should not fetch catalogs")

    monkeypatch.setattr("app.pricing.catalogs", fail)
    async with AsyncSessionLocal() as db:
        db.add(
            ModelPrice(
                model="gpt-example",
                input_usd_per_million=7,
                output_usd_per_million=9,
                source="manual",
            )
        )
        await db.commit()
        price = await ensure_model_price(db, "gpt-example")
        assert price.input_usd_per_million == 7 and price.source == "manual"


async def test_concurrent_first_prices_do_not_duplicate_or_lose_logs(key, monkeypatch):
    async def catalogs():
        return LITE, ROUTER

    monkeypatch.setattr("app.pricing.catalogs", catalogs)

    async def write():
        async with AsyncSessionLocal() as db:
            await record_usage(
                db,
                key,
                "/v1/responses",
                "gpt-example",
                200,
                {"usage": {"input_tokens": 1}},
                None,
            )

    await asyncio.gather(*(write() for _ in range(12)))
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(ModelPrice.id))) == 1
        assert await db.scalar(select(func.count(UsageLog.id))) == 12


async def test_unavailable_price_still_records_usage(key):
    async with AsyncSessionLocal() as db:
        await record_usage(
            db,
            key,
            "/v1/responses",
            "unknown-model",
            200,
            {"usage": {"input_tokens": 7}},
            None,
        )
    async with AsyncSessionLocal() as db:
        log = await db.scalar(select(UsageLog))
        assert log.prompt_tokens == 7 and log.cost_usd == 0


async def test_catalog_fetch_is_cached_and_failed_refresh_retains_snapshot(monkeypatch):
    import httpx

    import app.pricing as pricing

    monkeypatch.setattr(pricing, "_catalogs", ({}, []))
    monkeypatch.setattr(pricing, "_cache_until", 0)
    monkeypatch.setattr(pricing, "_lock", asyncio.Lock())
    requests = []
    failing = False

    class CatalogClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            requests.append(url)
            if failing:
                raise httpx.ConnectError("unavailable")
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json=LITE if url == pricing.LITELLM_URL else {"data": ROUTER},
            )

    monkeypatch.setattr(pricing.httpx, "AsyncClient", CatalogClient)
    results = await asyncio.gather(*(load_catalogs() for _ in range(10)))
    assert results[0] == (LITE, ROUTER)
    assert len(requests) == 2
    failing = True
    monkeypatch.setattr(pricing, "_cache_until", 0)
    assert await load_catalogs() == (LITE, ROUTER)
    assert len(requests) == 4
    assert await load_catalogs() == (LITE, ROUTER)
    assert len(requests) == 4
