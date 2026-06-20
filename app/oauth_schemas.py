from __future__ import annotations

from pydantic import BaseModel


class OAuthCompleteRequest(BaseModel):
    session_id: int
    callback_url: str
