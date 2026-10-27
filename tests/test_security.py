import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.config import settings
from app.crypto import PREFIX, decrypt
from app.db import AsyncSessionLocal, engine
from app.models import UpstreamCredential
from app.oauth_manual import get_active_upstream_credential
from app.codex_auth import extract_best_credential


async def test_default_admin_token_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, 'admin_token', 'change_me_admin_token')
    assert (await client.get('/admin/users', headers={'Authorization':'Bearer change_me_admin_token'})).status_code == 503
    with pytest.raises(ValueError, match='ADMIN_TOKEN'):
        settings.validate_runtime()


async def test_credentials_are_encrypted_in_sqlite():
    async with AsyncSessionLocal() as db:
        cred = UpstreamCredential(provider='codex', access_token='secret-access', refresh_token='secret-refresh', raw_json='{"secret":"value"}')
        db.add(cred)
        await db.commit()
    async with engine.connect() as conn:
        row = (await conn.execute(text('SELECT access_token, refresh_token, raw_json FROM upstream_credentials'))).one()
        assert all(value.startswith(PREFIX) for value in row)
        assert decrypt(row[0]) == 'secret-access'
    async with AsyncSessionLocal() as db:
        loaded = await db.get(UpstreamCredential, cred.id)
        assert loaded.refresh_token == 'secret-refresh'


async def test_legacy_plaintext_is_migrated():
    from app.migrations import initialize_database
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO upstream_credentials (provider, auth_mode, access_token, refresh_token, created_at, updated_at) VALUES ('codex', 'oauth_manual', 'old-access', 'old-refresh', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    await initialize_database()
    await initialize_database()
    async with engine.connect() as conn:
        value = await conn.scalar(text('SELECT access_token FROM upstream_credentials'))
        assert value.startswith(PREFIX)
        assert decrypt(value) == 'old-access'


async def test_only_one_refresh_for_concurrent_requests(monkeypatch):
    calls = []
    async def token_endpoint(payload):
        calls.append(payload)
        await asyncio.sleep(0.05)
        return {'access_token':'new-access', 'refresh_token':'rotated-refresh', 'expires_in':3600}
    monkeypatch.setattr('app.oauth_manual.post_oauth', token_endpoint)
    async with AsyncSessionLocal() as db:
        db.add(UpstreamCredential(provider='codex', access_token='old-access', refresh_token='old-refresh', expires_at=datetime.utcnow() - timedelta(seconds=1)))
        await db.commit()
    async def resolve():
        async with AsyncSessionLocal() as db:
            return (await get_active_upstream_credential(db)).access_token
    assert await asyncio.gather(resolve(), resolve(), resolve()) == ['new-access'] * 3
    assert len(calls) == 1


async def test_expired_credential_without_refresh_is_rejected():
    async with AsyncSessionLocal() as db:
        db.add(UpstreamCredential(provider='codex', access_token='expired', expires_at=datetime.utcnow() - timedelta(seconds=1)))
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await get_active_upstream_credential(db)
        assert error.value.status_code == 401


def test_auth_file_prefers_access_token_and_ignores_id_token():
    candidate = extract_best_credential({'tokens': {'id_token':'id.jwt.value', 'refresh_token':'refresh', 'access_token':'access.jwt.value'}})
    assert candidate.value == 'access.jwt.value'
    assert extract_best_credential({'id_token':'id.jwt.value', 'OPENAI_API_KEY':'sk-unrelated'}) is None
