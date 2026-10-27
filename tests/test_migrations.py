import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, text

from app.crypto import PREFIX, decrypt, encrypt
from app.db import engine
from app.migrations import initialize_database


async def insert_credentials(count: int):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO upstream_credentials (provider, auth_mode, access_token, refresh_token, raw_json, created_at, updated_at) VALUES ('codex', 'oauth_manual', :access, :refresh, :raw, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            [
                {"access": f"access-{i}", "refresh": f"refresh-{i}", "raw": "{}"}
                for i in range(count)
            ],
        )


async def test_startup_migration_batches_updates_and_is_idempotent():
    await insert_credentials(600)
    updates = []

    def capture(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append((statement, parameters, many))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await initialize_database()
        assert len(updates) == 3
        assert all(many for _, _, many in updates)
        assert sum(len(parameters) for _, parameters, _ in updates) == 600
        async with engine.connect() as conn:
            first = (
                await conn.execute(
                    text(
                        "SELECT id, access_token, refresh_token, raw_json FROM upstream_credentials ORDER BY id"
                    )
                )
            ).all()
        for i, row in enumerate(first):
            assert decrypt(row.access_token) == f"access-{i}"
            assert decrypt(row.refresh_token) == f"refresh-{i}"
            assert decrypt(row.raw_json) == "{}"
        updates.clear()
        await initialize_database()
        assert updates == []
        async with engine.connect() as conn:
            second = (
                await conn.execute(
                    text(
                        "SELECT id, access_token, refresh_token, raw_json FROM upstream_credentials ORDER BY id"
                    )
                )
            ).all()
        assert first == second
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)


async def test_oauth_cleanup_preserves_pending_verifier_and_does_not_reencrypt():
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO oauth_sessions (provider, state, code_verifier, authorize_url, status, callback_url, created_at) VALUES ('codex', :state, :verifier, 'https://example.invalid', :status, :callback, CURRENT_TIMESTAMP)"
            ),
            [
                {
                    "state": "pending-state",
                    "verifier": "pending-secret",
                    "status": "pending",
                    "callback": None,
                },
                {
                    "state": "completed-state",
                    "verifier": encrypt("old-verifier"),
                    "status": "completed",
                    "callback": "http://localhost/?code=private",
                },
            ],
        )
    await initialize_database()
    async with engine.connect() as conn:
        first = (
            await conn.execute(
                text(
                    "SELECT code_verifier, callback_url FROM oauth_sessions ORDER BY id"
                )
            )
        ).all()
    assert decrypt(first[0].code_verifier) == "pending-secret"
    assert decrypt(first[1].code_verifier) == ""
    assert first[1].callback_url == "received"
    await initialize_database()
    async with engine.connect() as conn:
        second = (
            await conn.execute(
                text(
                    "SELECT code_verifier, callback_url FROM oauth_sessions ORDER BY id"
                )
            )
        ).all()
    assert first == second


async def test_wrong_key_rolls_back_previous_migration_batches():
    await insert_credentials(300)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE upstream_credentials SET access_token = :value WHERE id = 300"
            ),
            {
                "value": PREFIX
                + Fernet(Fernet.generate_key()).encrypt(b"foreign").decode()
            },
        )
    with pytest.raises(ValueError, match="original credential encryption key"):
        await initialize_database()
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT access_token FROM upstream_credentials WHERE id = 1")
            )
            == "access-0"
        )
