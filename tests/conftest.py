import base64
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

_temp = tempfile.TemporaryDirectory(prefix="looper-tests-")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(Path(_temp.name) / "test.db")
os.environ["ADMIN_TOKEN"] = "test-admin-token-with-enough-entropy"
os.environ["CREDENTIAL_KEY_FILE"] = str(Path(_temp.name) / "credential.key")

from app.db import AsyncSessionLocal, Base, engine
from app.main import app
from app.models import ApiKey, User
from app.utils import sha256_text


@pytest.fixture
def make_jwt():
    def encode(payload):
        encoded = (
            base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        )
        return f"header.{encoded}.signature"

    return encode


@pytest_asyncio.fixture(autouse=True)
async def database():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield client


@pytest_asyncio.fixture
async def key():
    async with AsyncSessionLocal() as db:
        user = User(name="test-user")
        db.add(user)
        await db.flush()
        key = ApiKey(
            user_id=user.id,
            name="test-key",
            key_hash=sha256_text("gtw_test"),
            key_preview="gtw_...test",
        )
        db.add(key)
        await db.commit()
        return key
