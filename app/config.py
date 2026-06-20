from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = Field(default='codex-gateway-python', alias='APP_NAME')
    host: str = Field(default='0.0.0.0', alias='HOST')
    port: int = Field(default=8080, alias='PORT')
    admin_token: str = Field(default='change_me_admin_token', alias='ADMIN_TOKEN')
    database_url: str = Field(default='sqlite+aiosqlite:///./data/gateway.db', alias='DATABASE_URL')

    upstream_base_url: str = Field(default='https://chatgpt.com/backend-api', alias='UPSTREAM_BASE_URL')
    upstream_auth_mode: str = Field(default='oauth_manual', alias='UPSTREAM_AUTH_MODE')
    upstream_bearer_token: str = Field(default='', alias='UPSTREAM_BEARER_TOKEN')
    upstream_timeout_seconds: int = Field(default=120, alias='UPSTREAM_TIMEOUT_SECONDS')
    upstream_transport: str = Field(default='sse', alias='UPSTREAM_TRANSPORT')
    upstream_openai_beta: str = Field(default='responses=experimental', alias='UPSTREAM_OPENAI_BETA')
    default_instructions: str = Field(default='You are a helpful assistant.', alias='DEFAULT_INSTRUCTIONS')
    default_allowed_models: str = Field(default='gpt-5.4,gpt-5.3,gpt-5.1-codex-mini,gpt-5.3-codex', alias='DEFAULT_ALLOWED_MODELS')

    oauth_authorize_url: str = Field(default='https://auth.openai.com/oauth/authorize', alias='OAUTH_AUTHORIZE_URL')
    oauth_token_url: str = Field(default='https://auth.openai.com/oauth/token', alias='OAUTH_TOKEN_URL')
    oauth_client_id: str = Field(default='app_EMoamEEZ73f0CkXaXp7hrann', alias='OAUTH_CLIENT_ID')
    oauth_client_secret: str = Field(default='', alias='OAUTH_CLIENT_SECRET')
    oauth_redirect_uri: str = Field(default='http://localhost:1455/auth/callback', alias='OAUTH_REDIRECT_URI')
    oauth_scope: str = Field(default='openid profile email offline_access', alias='OAUTH_SCOPE')
    oauth_audience: str = Field(default='', alias='OAUTH_AUDIENCE')
    oauth_originator: str = Field(default='pi', alias='OAUTH_ORIGINATOR')
    oauth_codex_cli_simplified_flow: bool = Field(default=True, alias='OAUTH_CODEX_CLI_SIMPLIFIED_FLOW')
    oauth_id_token_add_organizations: bool = Field(default=True, alias='OAUTH_ID_TOKEN_ADD_ORGANIZATIONS')
    oauth_session_ttl_minutes: int = Field(default=15, alias='OAUTH_SESSION_TTL_MINUTES')

    codex_command: str = Field(default='codex', alias='CODEX_COMMAND')
    codex_login_command: str = Field(default='', alias='CODEX_LOGIN_COMMAND')
    codex_home: str = Field(default='~/.codex', alias='CODEX_HOME')
    codex_auth_credentials_store: str = Field(default='file', alias='CODEX_AUTH_CREDENTIALS_STORE')
    codex_login_log_file: str = Field(default='./data/codex-login.log', alias='CODEX_LOGIN_LOG_FILE')

    @property
    def allowed_models(self) -> list[str]:
        if not self.default_allowed_models.strip():
            return []
        return [x.strip() for x in self.default_allowed_models.split(',') if x.strip()]


settings = Settings()
