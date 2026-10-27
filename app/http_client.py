import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import settings

_client: httpx.AsyncClient | None = None


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    access_token: str = Field(min_length=1, strict=True)
    refresh_token: str | None = None
    expires_in: int = Field(default=3600, gt=0)
    token_type: str = "Bearer"
    scope: str | None = None
    account_id: str | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=settings.upstream_timeout_seconds,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def post_oauth(data: dict) -> dict:
    from fastapi import HTTPException

    try:
        response = await get_http_client().post(settings.oauth_token_url, data=data)
    except httpx.TimeoutException:
        raise HTTPException(504, "OAuth endpoint timed out") from None
    except httpx.RequestError:
        raise HTTPException(502, "OAuth endpoint is unavailable") from None
    if response.status_code >= 400:
        raise HTTPException(
            401 if data["grant_type"] == "refresh_token" else 502,
            "OAuth token request failed; please login again",
        )
    try:
        result = TokenResponse.model_validate(response.json()).model_dump()
    except (ValueError, ValidationError):
        raise HTTPException(502, "OAuth endpoint returned invalid JSON") from None
    return result
