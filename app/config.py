from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.model_utils import normalize_model_list


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = Field(default="Looper-AI-Gateway", alias="APP_NAME")
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    admin_token: str = Field(default="", alias="ADMIN_TOKEN")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/gateway.db", alias="DATABASE_URL"
    )
    credential_encryption_key: str = Field(
        default="", alias="CREDENTIAL_ENCRYPTION_KEY"
    )
    credential_key_file: str = Field(
        default="./data/credential.key", alias="CREDENTIAL_KEY_FILE"
    )

    upstream_base_url: str = Field(
        default="https://chatgpt.com/backend-api", alias="UPSTREAM_BASE_URL"
    )
    upstream_auth_mode: Literal["oauth_manual", "codex_auth_file", "env_token"] = Field(
        default="oauth_manual", alias="UPSTREAM_AUTH_MODE"
    )
    upstream_bearer_token: str = Field(default="", alias="UPSTREAM_BEARER_TOKEN")
    upstream_timeout_seconds: int = Field(
        default=120, alias="UPSTREAM_TIMEOUT_SECONDS", gt=0
    )
    upstream_proxy_url: str = Field(default="", alias="UPSTREAM_PROXY_URL")
    upstream_max_attempts: int = Field(
        default=3, alias="UPSTREAM_MAX_ATTEMPTS", ge=1, le=10
    )
    upstream_failure_cooldown_seconds: int = Field(
        default=10, alias="UPSTREAM_FAILURE_COOLDOWN_SECONDS", ge=1, le=3600
    )
    upstream_rate_limit_cooldown_seconds: int = Field(
        default=60, alias="UPSTREAM_RATE_LIMIT_COOLDOWN_SECONDS", ge=1, le=86400
    )
    upstream_capacity_wait_seconds: float = Field(
        default=15, alias="UPSTREAM_CAPACITY_WAIT_SECONDS", ge=0, le=120
    )
    max_request_bytes: int = Field(
        default=16 * 1024 * 1024, alias="MAX_REQUEST_BYTES", gt=0
    )
    upstream_transport: Literal["sse"] = Field(
        default="sse", alias="UPSTREAM_TRANSPORT"
    )
    upstream_openai_beta: str = Field(
        default="responses=experimental", alias="UPSTREAM_OPENAI_BETA"
    )
    default_instructions: str = Field(
        default="You are a helpful assistant.", alias="DEFAULT_INSTRUCTIONS"
    )
    model_catalog: str = Field(
        default="gpt-6.1-sol",
        alias="MODEL_CATALOG",
    )

    oauth_authorize_url: str = Field(
        default="https://auth.openai.com/oauth/authorize", alias="OAUTH_AUTHORIZE_URL"
    )
    oauth_token_url: str = Field(
        default="https://auth.openai.com/oauth/token", alias="OAUTH_TOKEN_URL"
    )
    oauth_client_id: str = Field(
        default="app_EMoamEEZ73f0CkXaXp7hrann", alias="OAUTH_CLIENT_ID"
    )
    oauth_client_secret: str = Field(default="", alias="OAUTH_CLIENT_SECRET")
    oauth_redirect_uri: str = Field(
        default="http://localhost:1455/auth/callback", alias="OAUTH_REDIRECT_URI"
    )
    oauth_scope: str = Field(
        default="openid profile email offline_access", alias="OAUTH_SCOPE"
    )
    oauth_audience: str = Field(default="", alias="OAUTH_AUDIENCE")
    oauth_originator: str = Field(default="pi", alias="OAUTH_ORIGINATOR")
    oauth_codex_cli_simplified_flow: bool = Field(
        default=True, alias="OAUTH_CODEX_CLI_SIMPLIFIED_FLOW"
    )
    oauth_id_token_add_organizations: bool = Field(
        default=True, alias="OAUTH_ID_TOKEN_ADD_ORGANIZATIONS"
    )
    oauth_session_ttl_minutes: int = Field(
        default=15, alias="OAUTH_SESSION_TTL_MINUTES", gt=0
    )

    codex_command: str = Field(default="codex", alias="CODEX_COMMAND")
    codex_login_command: str = Field(default="", alias="CODEX_LOGIN_COMMAND")
    codex_home: str = Field(default="~/.codex", alias="CODEX_HOME")
    codex_auth_credentials_store: str = Field(
        default="file", alias="CODEX_AUTH_CREDENTIALS_STORE"
    )
    codex_login_log_file: str = Field(
        default="./data/codex-login.log", alias="CODEX_LOGIN_LOG_FILE"
    )

    @field_validator("upstream_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        parsed = urlsplit(value)
        # HTTP CONNECT proxies also support HTTPS destinations; SOCKS would
        # require an additional optional dependency that this image omits.
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("UPSTREAM_PROXY_URL must be an HTTP(S) proxy URL")
        _ = parsed.port  # Validate malformed or out-of-range port numbers.
        return value

    def validate_runtime(self) -> None:
        if not self.admin_token_is_secure:
            raise ValueError(
                "Set ADMIN_TOKEN to a random value of at least 32 characters before starting the server"
            )

    @property
    def admin_token_is_secure(self) -> bool:
        return len(self.admin_token.strip()) >= 32

    @property
    def catalog_models(self) -> list[str]:
        return normalize_model_list(self.model_catalog.split(","))


settings = Settings()
