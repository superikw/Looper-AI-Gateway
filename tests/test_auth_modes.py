from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import Settings, settings
from app.services import resolve_upstream_credential


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("env_token", ("env-token", None)),
        ("codex_auth_file", ("file-token", None)),
        ("oauth_manual", ("oauth-token", "account")),
    ],
)
async def test_auth_modes_use_only_selected_source(monkeypatch, mode, expected):
    monkeypatch.setattr(settings, "upstream_auth_mode", mode)
    monkeypatch.setattr(settings, "upstream_bearer_token", "env-token")

    def file_token():
        assert mode == "codex_auth_file"
        return "file-token"

    async def credential(db):
        assert mode == "oauth_manual"
        return SimpleNamespace(access_token="oauth-token", account_id="account")

    monkeypatch.setattr("app.services.codex_auth_manager.load_bearer_token", file_token)
    monkeypatch.setattr("app.services.get_active_upstream_credential", credential)
    assert await resolve_upstream_credential(object()) == expected


async def test_missing_oauth_does_not_fall_back_to_env_or_file(monkeypatch):
    monkeypatch.setattr(settings, "upstream_auth_mode", "oauth_manual")
    monkeypatch.setattr(settings, "upstream_bearer_token", "env-token")

    async def missing(db):
        return None

    def forbidden_file():
        pytest.fail("unexpected auth-file fallback")

    monkeypatch.setattr("app.services.get_active_upstream_credential", missing)
    monkeypatch.setattr(
        "app.services.codex_auth_manager.load_bearer_token", forbidden_file
    )
    with pytest.raises(HTTPException) as error:
        await resolve_upstream_credential(object())
    assert error.value.status_code == 401


def test_unknown_auth_mode_is_rejected_by_configuration():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, UPSTREAM_AUTH_MODE="auto")
