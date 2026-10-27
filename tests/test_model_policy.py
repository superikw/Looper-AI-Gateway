import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

import manage
from app.config import Settings, settings
from app.db import AsyncSessionLocal, engine
from app.migrations import initialize_database
from app.models import BlockedModel, UpstreamCredential, UsageLog

AUTH = {"Authorization": "Bearer gtw_test"}
ADMIN = {"Authorization": "Bearer test-admin-token-with-enough-entropy"}
BLACKLIST = "/admin/models/blacklist"


@pytest.fixture
async def upstream(monkeypatch):
    requests = []
    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamCredential(
                provider="codex", access_token="mock-token", account_id="mock-account"
            )
        )
        await db.commit()

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"type":"response.completed","response":{"id":"resp_test",'
            '"status":"completed","output":[],"usage":{"input_tokens":1,'
            '"output_tokens":1,"total_tokens":2}}}\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("app.services.get_http_client", lambda: client)
        yield requests


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
async def test_unlisted_models_allowed_and_blacklist_changes_apply_immediately(
    client, key, upstream, monkeypatch, path, stream
):
    monkeypatch.setattr(settings, "model_catalog", "catalog-model")
    payload = {"model": "CUSTOM_MODEL/V2", "stream": stream}
    payload.update(
        {"input": "hello"}
        if path == "/v1/responses"
        else {"messages": [{"role": "user", "content": "hello"}]}
    )
    assert (await client.post(path, headers=AUTH, json=payload)).status_code == 200
    assert upstream[-1]["model"] == "custom-model/v2"

    blocked = await client.post(
        BLACKLIST, headers=ADMIN, json={"model": " custom_model/V2 "}
    )
    assert blocked.status_code == 200
    assert blocked.json()["model"] == "custom-model/v2"
    denied = await client.post(path, headers=AUTH, json=payload)
    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == "model blocked: custom-model/v2"
    assert len(upstream) == 1
    async with AsyncSessionLocal() as db:
        log = await db.scalar(select(UsageLog).where(UsageLog.status_code == 403))
        assert log.model == "custom-model/v2"
        assert log.upstream_credential_id is None
        assert (await db.get(UpstreamCredential, 1)).request_count == 1

    removed = await client.delete(
        BLACKLIST, headers=ADMIN, params={"model": "CUSTOM_MODEL/V2"}
    )
    assert removed.status_code == 200
    assert (await client.post(path, headers=AUTH, json=payload)).status_code == 200
    assert len(upstream) == 2


async def test_catalog_filters_blacklist_without_becoming_an_allowlist(
    client, key, monkeypatch
):
    monkeypatch.setattr(settings, "model_catalog", "GPT6.1_sol,other-model,GPT6.1_sol")
    assert (await client.get("/v1/models", headers=AUTH)).json()["data"] == [
        {"id": "gpt-6.1-sol", "object": "model"},
        {"id": "other-model", "object": "model"},
    ]
    await client.post(BLACKLIST, headers=ADMIN, json={"model": "GPT6.1_sol"})
    assert (await client.get("/v1/models", headers=AUTH)).json()["data"] == [
        {"id": "other-model", "object": "model"}
    ]
    monkeypatch.setattr(settings, "model_catalog", "")
    assert (await client.get("/v1/models", headers=AUTH)).json()["data"] == []


async def test_blacklist_admin_operations_are_idempotent_and_persistent(client):
    added = await asyncio.gather(
        *[
            client.post(BLACKLIST, headers=ADMIN, json={"model": name})
            for name in ["GPT6.1_sol", "gpt-6.1-sol"]
        ]
    )
    assert all(response.status_code == 200 for response in added)
    assert added[0].json() == added[1].json()
    await initialize_database()
    listed = await client.get(BLACKLIST, headers=ADMIN)
    assert listed.json() == [added[0].json()]
    for _ in range(2):
        removed = await client.delete(
            BLACKLIST, headers=ADMIN, params={"model": "gpt-6.1-sol"}
        )
        assert removed.status_code == 200
    assert (await client.get(BLACKLIST, headers=ADMIN)).json() == []


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("headers", [{}, AUTH])
async def test_blacklist_requires_admin(client, key, method, headers):
    response = await client.request(
        method,
        BLACKLIST,
        headers=headers,
        params={"model": "test-model"},
        json={"model": "test-model"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("model", ["", " ", "x" * 129, None, 42])
async def test_blacklist_rejects_invalid_models(client, model):
    response = await client.post(BLACKLIST, headers=ADMIN, json={"model": model})
    assert response.status_code in (400, 422)
    assert (await client.get(BLACKLIST, headers=ADMIN)).json() == []


async def test_cli_and_admin_share_live_blacklist(client, key, capsys):
    async def run(command):
        args = manage.build_parser().parse_args(command)
        await args.func(args)
        return json.loads(capsys.readouterr().out)

    assert await run(["list-blocked-models"]) == []
    added = await run(["block-model", "GPT6.1_sol"])
    assert added["model"] == "gpt-6.1-sol"
    assert (await client.get(BLACKLIST, headers=ADMIN)).json() == [added]
    assert (
        await client.post(
            "/v1/responses",
            headers=AUTH,
            json={"model": "gpt-6.1-sol", "input": "hello"},
        )
    ).status_code == 403
    assert await run(["block-model", "gpt-6.1-sol"]) == added
    assert await run(["list-blocked-models"]) == [added]
    await run(["unblock-model", "GPT6.1_sol"])
    assert (await client.get(BLACKLIST, headers=ADMIN)).json() == []
    args = manage.build_parser().parse_args(["block-model", " "])
    with pytest.raises(HTTPException):
        await args.func(args)


async def test_upgrade_creates_empty_blacklist_without_losing_existing_keys(key):
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE blocked_models"))
    await initialize_database()
    await initialize_database()
    async with AsyncSessionLocal() as db:
        assert (await db.scalars(select(BlockedModel))).all() == []
        assert (
            await db.scalar(
                text("SELECT key_hash FROM api_keys WHERE id = :id"), {"id": key.id}
            )
            == key.key_hash
        )
        assert (
            await db.scalar(
                text("SELECT count(*) FROM schema_migrations WHERE version = 6")
            )
            == 1
        )


def test_legacy_allowlist_no_longer_controls_model_access():
    config = Settings(
        _env_file=None, DEFAULT_ALLOWED_MODELS="old-model", MODEL_CATALOG="new-model"
    )
    assert config.catalog_models == ["new-model"]
    assert "default_allowed_models" not in Settings.model_fields
