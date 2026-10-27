import httpx
import pytest

from app.db import AsyncSessionLocal
from app.models import UpstreamCredential


async def test_readiness_requires_upstream_login(client):
    assert (await client.get("/healthz")).status_code == 200
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": True, "upstream_auth": False}
    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamCredential(
                provider="codex", access_token="mock-token", account_id="mock-account"
            )
        )
        await db.commit()
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert "mock-token" not in response.text
    assert "mock-account" not in response.text


@pytest.mark.parametrize("mode", ["env_token", "codex_auth_file", "oauth_manual"])
@pytest.mark.parametrize("has_account", [False, True])
async def test_readiness_matches_forwarding_requirements(
    client, monkeypatch, make_jwt, mode, has_account
):
    from fastapi import HTTPException

    from app.config import settings
    from app.services import open_upstream
    from app.token_utils import JWT_CLAIM_PATH

    token = (
        make_jwt({JWT_CLAIM_PATH: {"chatgpt_account_id": "routing-account"}})
        if has_account
        else "opaque-token"
    )
    monkeypatch.setattr(settings, "upstream_auth_mode", mode)
    monkeypatch.setattr(settings, "upstream_bearer_token", token)
    monkeypatch.setattr(
        "app.services.codex_auth_manager.load_bearer_token", lambda: token
    )
    if mode == "oauth_manual":
        async with AsyncSessionLocal() as db:
            db.add(UpstreamCredential(provider="codex", access_token=token))
            await db.commit()
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"output": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    ) as upstream_client:
        monkeypatch.setattr("app.services.get_http_client", lambda: upstream_client)
        readiness = await client.get("/readyz")
        assert readiness.status_code == (200 if has_account else 503)
        assert readiness.json()["checks"]["upstream_auth"] is has_account
        assert requests == []  # Readiness never sends a model request.
        async with AsyncSessionLocal() as db:
            payload = {
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
            }
            if has_account:
                response = await open_upstream("/v1/chat/completions", payload, db)
                await response.aclose()
                assert requests[0].headers["chatgpt-account-id"] == "routing-account"
            else:
                with pytest.raises(HTTPException) as error:
                    await open_upstream("/v1/chat/completions", payload, db)
                assert error.value.status_code == 502
                assert requests == []


async def test_readiness_rejects_unconfigured_env_token(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "upstream_auth_mode", "env_token")
    monkeypatch.setattr(settings, "upstream_bearer_token", "")
    assert (await client.get("/readyz")).status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"access_token": 42},
        {"access_token": "secret", "expires_in": -1},
        {"refresh_token": "secret"},
    ],
)
async def test_invalid_oauth_response_is_gateway_error(monkeypatch, body):
    from fastapi import HTTPException

    from app.http_client import post_oauth

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        monkeypatch.setattr("app.http_client.get_http_client", lambda: client)
        with pytest.raises(HTTPException) as error:
            await post_oauth({"grant_type": "refresh_token"})
        assert error.value.status_code == 502
        assert "secret" not in str(error.value.detail)
