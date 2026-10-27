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


async def test_legacy_credentials_gain_soft_delete_column_without_data_loss():
    await insert_credentials(2)
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE upstream_credentials DROP COLUMN deleted_at")
        )
    await initialize_database()
    await initialize_database()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT access_token, deleted_at FROM upstream_credentials ORDER BY id"
                )
            )
        ).all()
        assert [decrypt(row.access_token) for row in rows] == ["access-0", "access-1"]
        assert all(row.deleted_at is None for row in rows)
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM schema_migrations WHERE version = 3")
            )
            == 1
        )


async def test_upgrade_adds_pool_fields_and_preserves_legacy_data(key):
    await insert_credentials(1)
    async with engine.begin() as conn:
        for field in (
            "name",
            "enabled",
            "cooldown_until",
            "last_used_at",
            "request_count",
            "failure_count",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            await conn.execute(
                text(f"ALTER TABLE upstream_credentials DROP COLUMN {field}")
            )
        await conn.execute(text("DROP INDEX ix_usage_logs_upstream_credential_id"))
        # SQLite cannot DROP a column referenced by a table-level FK.
        # Rebuild the pre-pool schema while preserving its keys and data.
        for table in ("api_keys", "usage_logs"):
            ddl = await conn.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"
                ),
                {"name": table},
            )
            ddl = "\n".join(
                line
                for line in ddl.splitlines()
                if "upstream_credential_id" not in line
            )
            ddl = ddl.rstrip()[:-1].rstrip().rstrip(",") + "\n)"
            ddl = ddl.replace(
                f"CREATE TABLE {table}", f"CREATE TABLE legacy_{table}", 1
            )
            await conn.execute(text(ddl))
            columns = [
                row[1]
                for row in await conn.execute(text(f"PRAGMA table_info({table})"))
                if row[1] != "upstream_credential_id"
            ]
            names = ", ".join(columns)
            await conn.execute(
                text(
                    f"INSERT INTO legacy_{table} ({names}) SELECT {names} FROM {table}"
                )
            )
        await conn.execute(text("DROP TABLE usage_logs"))
        await conn.execute(text("DROP TABLE api_keys"))
        for table in ("api_keys", "usage_logs"):
            await conn.execute(text(f"ALTER TABLE legacy_{table} RENAME TO {table}"))
    await initialize_database()
    await initialize_database()
    async with engine.connect() as conn:
        row = (
            (await conn.execute(text("SELECT * FROM upstream_credentials")))
            .mappings()
            .one()
        )
        assert decrypt(row["access_token"]) == "access-0"
        assert row["enabled"] == 1
        assert row["name"] is None and row["cooldown_until"] is None
        assert row["request_count"] == row["total_tokens"] == row["failure_count"] == 0
        saved = (await conn.execute(text("SELECT * FROM api_keys"))).mappings().one()
        assert saved["id"] == key.id and saved["key_hash"] == key.key_hash
        assert saved["upstream_credential_id"] is None
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM schema_migrations WHERE version = 4")
            )
            == 1
        )
