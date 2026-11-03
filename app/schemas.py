from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ModelBlockCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128)


class ModelBlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    model: str
    created_at: datetime


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=255)


class UserOut(BaseModel):
    id: int
    name: str
    note: str | None
    enabled: bool = True
    deleted_at: datetime | None = None
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


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class AdminPasswordChange(BaseModel):
    current_password: str | None = Field(default=None, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class AdminCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128)


class AdminOut(BaseModel):
    id: int
    username: str
    is_superadmin: bool
    must_change_password: bool
    enabled: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ModelPriceIn(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    input_usd_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_usd_per_million: float = Field(ge=0, allow_inf_nan=False)
    cached_input_usd_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    source: str = Field(default="manual", max_length=255)


class SettingUpdate(BaseModel):
    timezone: str | None = Field(default=None, max_length=128)
    log_retention_days: int | None = Field(default=None, ge=1, le=3650)


class UserProvision(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=255)
    key_name: str = Field(default="default", min_length=1, max_length=128)


class ApiKeyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upstream_credential_id: int | None = Field(gt=0)


class UpstreamAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: StrictBool | None = None
    max_rpm: int = Field(default=30, strict=True, ge=1, le=100000)
    max_concurrency: int = Field(default=4, strict=True, ge=1, le=10000)
    min_interval_seconds: float = Field(
        default=0, strict=True, ge=0, le=3600, allow_inf_nan=False
    )
    reset_cooldown: StrictBool = False


class UserRouteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upstream_ids: list[int] = Field(max_length=1000)


class UpstreamRouteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: list[int] = Field(max_length=1000)
