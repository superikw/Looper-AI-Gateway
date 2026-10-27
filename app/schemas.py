from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=255)


class UserOut(BaseModel):
    id: int
    name: str
    note: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApiKeyCreate(BaseModel):
    user_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=128)


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

    model_config = ConfigDict(from_attributes=True)
