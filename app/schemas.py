from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


class UserCreate(BaseModel):
    name: str
    note: str | None = None


class UserOut(BaseModel):
    id: int
    name: str
    note: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class ApiKeyCreate(BaseModel):
    user_id: int
    name: str


class ApiKeyCreated(BaseModel):
    id: int
    user_id: int
    name: str
    api_key: str
    key_preview: str
    enabled: bool
    created_at: datetime


class ApiKeyOut(BaseModel):
    id: int
    user_id: int
    name: str
    key_preview: str
    enabled: bool
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    created_at: datetime

    class Config:
        from_attributes = True
