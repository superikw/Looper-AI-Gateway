from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool


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
    upstream_credential_id: int | None = Field(default=None, gt=0)


class ApiKeyCreated(BaseModel):
    id: int
    user_id: int
    name: str
    api_key: str
    key_preview: str
    enabled: bool
    created_at: datetime
    upstream_credential_id: int | None = None


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
    upstream_credential_id: int | None = None

    model_config = ConfigDict(from_attributes=True)


class ApiKeyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upstream_credential_id: int | None = Field(gt=0)


class UpstreamAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: StrictBool | None = None
    reset_cooldown: StrictBool = False
