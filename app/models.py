from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.crypto import EncryptedText
from app.db import Base
from app.utils import utcnow


class BlockedModel(Base):
    __tablename__ = "blocked_models"

    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    max_concurrency: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    max_rpm: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    min_interval_seconds: Mapped[float] = mapped_column(
        Float, default=0, server_default="0"
    )

    routing_configured: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_preview: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    upstream_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("upstream_credentials.id"), nullable=True
    )
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="api_keys")
    usage_logs: Mapped[list["UsageLog"]] = relationship(back_populates="api_key")


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    upstream_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("upstream_credentials.id"), nullable=True, index=True
    )
    path: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_final: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    upstream_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    gateway_request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    upstream_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    api_key: Mapped["ApiKey"] = relationship(back_populates="usage_logs")


class AdminAccount(Base):
    __tablename__ = "admin_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_superadmin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ModelPrice(Base):
    __tablename__ = "model_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    input_usd_per_million: Mapped[float] = mapped_column(Float, default=0)
    output_usd_per_million: Mapped[float] = mapped_column(Float, default=0)
    cached_input_usd_per_million: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(255), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ActiveSession(Base):
    __tablename__ = "active_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), index=True)
    path: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_event_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OAuthSession(Base):
    __tablename__ = "oauth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    code_verifier: Mapped[str] = mapped_column(EncryptedText())
    authorize_url: Mapped[str] = mapped_column(Text)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UpstreamCredential(Base):
    __tablename__ = "upstream_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    auth_mode: Mapped[str] = mapped_column(String(32), default="oauth_manual")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    max_rpm: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4, server_default="4")
    min_interval_seconds: Mapped[float] = mapped_column(
        Float, default=0, server_default="0"
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    upstream_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    upstream_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_token: Mapped[str] = mapped_column(EncryptedText())
    refresh_token: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    token_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UpstreamRequest(Base):
    __tablename__ = "upstream_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    upstream_credential_id: Mapped[int] = mapped_column(
        ForeignKey("upstream_credentials.id"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    dispatched: Mapped[bool] = mapped_column(Boolean, default=False)


class UserUpstreamRoute(Base):
    __tablename__ = "user_upstream_routes"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    upstream_credential_id: Mapped[int] = mapped_column(
        ForeignKey("upstream_credentials.id"), primary_key=True, index=True
    )


class UserRequest(Base):
    __tablename__ = "user_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class UpstreamAttempt(Base):
    __tablename__ = "upstream_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gateway_request_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path: Mapped[str] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    upstream_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upstream_error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    upstream_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transport_exception_type: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    retry_decision: Mapped[str] = mapped_column(String(64))
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
