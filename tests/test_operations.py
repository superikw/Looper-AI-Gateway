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
