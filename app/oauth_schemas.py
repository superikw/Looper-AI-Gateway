from __future__ import annotations

from pydantic import BaseModel, Field


class OAuthCompleteRequest(BaseModel):
    session_id: int
    callback_url: str
    name: str | None = Field(default=None, min_length=1, max_length=128)
