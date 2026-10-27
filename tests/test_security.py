import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.codex_auth import extract_best_credential
from app.config import settings
from app.crypto import PREFIX, decrypt
from app.db import AsyncSessionLocal, engine
from app.models import UpstreamCredential
from app.oauth_manual import get_active_upstream_credential
from app.utils import utcnow


@pytest.mark.parametrize("token", ["", " " * 32, "short-token", "x" * 31])
async def test_unconfigured_or_short_admin_token_is_rejected(
    client, monkeypatch, token
):
    monkeypatch.setattr(settings, "admin_token", token)
    assert (
        await client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 503
    with pytest.raises(ValueError, match="ADMIN_TOKEN"):
        settings.validate_runtime()


async def test_credentials_are_encrypted_in_sqlite():
    async with AsyncSessionLocal() as db:
        cred = UpstreamCredential(
            provider="codex",
            access_token="secret-access",
            refresh_token="secret-refresh",
            raw_json='{"secret":"value"}',
        )
        db.add(cred)
        await db.commit()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT access_token, refresh_token, raw_json FROM upstream_credentials"
                )
            )
        ).one()
        assert all(value.startswith(PREFIX) for value in row)
        assert decrypt(row[0]) == "secret-access"
    async with AsyncSessionLocal() as db:
        loaded = await db.get(UpstreamCredential, cred.id)
        assert loaded.refresh_token == "secret-refresh"


async def test_legacy_plaintext_is_migrated():
    from app.migrations import initialize_database

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO upstream_credentials (provider, auth_mode, access_token, refresh_token, created_at, updated_at) VALUES ('codex', 'oauth_manual', 'old-access', 'old-refresh', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    await initialize_database()
    await initialize_database()
    async with engine.connect() as conn:
        value = await conn.scalar(text("SELECT access_token FROM upstream_credentials"))
        assert value.startswith(PREFIX)
        assert decrypt(value) == "old-access"


async def test_only_one_refresh_for_concurrent_requests(monkeypatch):
    calls = []

    async def token_endpoint(payload):
        calls.append(payload)
        await asyncio.sleep(0.05)
        return {
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr("app.oauth_manual.post_oauth", token_endpoint)
    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamCredential(
                provider="codex",
                access_token="old-access",
                refresh_token="old-refresh",
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        await db.commit()

    async def resolve():
        async with AsyncSessionLocal() as db:
            return (await get_active_upstream_credential(db)).access_token

    assert await asyncio.gather(resolve(), resolve(), resolve()) == ["new-access"] * 3
    assert len(calls) == 1


async def test_expired_credential_without_refresh_is_rejected():
    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamCredential(
                provider="codex",
                access_token="expired",
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await get_active_upstream_credential(db)
        assert error.value.status_code == 401


def test_auth_file_prefers_access_token_and_ignores_id_token():
    candidate = extract_best_credential(
        {
            "tokens": {
                "id_token": "id.jwt.value",
                "refresh_token": "refresh",
                "access_token": "access.jwt.value",
            }
        }
    )
    assert candidate.value == "access.jwt.value"
    assert (
        extract_best_credential(
            {"id_token": "id.jwt.value", "OPENAI_API_KEY": "sk-unrelated"}
        )
        is None
    )


async def test_cross_process_refresh_uses_one_rotating_token(tmp_path):
    import sys

    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamCredential(
                provider="codex",
                access_token="old-access",
                refresh_token="old-refresh",
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        await db.commit()
    marker = tmp_path / "refresh-calls"
    program = """
import asyncio, sys
from pathlib import Path
from app import oauth_manual
from app.db import AsyncSessionLocal, engine
async def token_endpoint(payload):
    with Path(sys.argv[1]).open('a') as log:
        log.write('refresh\\n')
    await asyncio.sleep(0.1)
    return {'access_token':'new-access', 'refresh_token':'rotated-refresh', 'expires_in':3600}
oauth_manual.post_oauth = token_endpoint
async def run():
    async with AsyncSessionLocal() as db:
        credential = await oauth_manual.get_active_upstream_credential(db)
        print(credential.access_token)
    await engine.dispose()
asyncio.run(run())
"""
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            str(marker),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(3)
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(process.communicate() for process in processes)), 15
        )
        for process, (stdout, stderr) in zip(processes, results):
            assert process.returncode == 0, stderr.decode()
            assert stdout.decode().strip() == "new-access"
        assert marker.read_text().splitlines() == ["refresh"]
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_wrong_encryption_key_fails_migration(monkeypatch):
    from cryptography.fernet import Fernet

    from app.migrations import initialize_database

    async with AsyncSessionLocal() as db:
        db.add(UpstreamCredential(provider="codex", access_token="secret"))
        await db.commit()
    monkeypatch.setattr(
        settings, "credential_encryption_key", Fernet.generate_key().decode()
    )
    with pytest.raises(ValueError, match="original credential encryption key"):
        await initialize_database()


async def test_key_file_has_private_permissions():
    from pathlib import Path

    from app.crypto import cipher

    cipher()
    assert Path(settings.credential_key_file).stat().st_mode & 0o777 == 0o600


async def test_oauth_flow_does_not_keep_authorization_code(client, monkeypatch):
    from sqlalchemy import select

    from app.models import OAuthSession

    async def exchange(code, verifier):
        assert code == "private-code"
        assert len(verifier) >= 43
        return {
            "access_token": "mock-access",
            "refresh_token": "mock-refresh",
            "account_id": "mock-account",
            "expires_in": 3600,
        }

    monkeypatch.setattr("app.oauth_manual.exchange_code_for_token", exchange)
    auth = {"Authorization": "Bearer test-admin-token-with-enough-entropy"}
    started = (await client.post("/admin/auth/codex/oauth/start", headers=auth)).json()
    callback = (
        "http://localhost:1455/auth/callback?code=private-code&state="
        + started["state"]
    )
    response = await client.post(
        "/admin/auth/codex/oauth/complete",
        headers=auth,
        json={"session_id": started["session_id"], "callback_url": callback},
    )
    assert response.status_code == 200
    async with AsyncSessionLocal() as db:
        session = await db.scalar(select(OAuthSession))
        assert session.callback_url == "received"
        assert session.code_verifier == ""
        assert session.status == "completed"
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT callback_url, code_verifier FROM oauth_sessions")
            )
        ).one()
        assert "private-code" not in str(row)
        assert row.code_verifier.startswith(PREFIX)
