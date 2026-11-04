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
            "max_rpm",
            "max_concurrency",
            "min_interval_seconds",
            "cooldown_until",
            "last_used_at",
            "last_dispatched_at",
            "request_count",
            "failure_count",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            await conn.execute(
                text(f"ALTER TABLE upstream_credentials DROP COLUMN {field}")
            )
        await conn.execute(text("DROP TABLE upstream_requests"))
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
        assert (
            row["max_rpm"],
            row["max_concurrency"],
            row["min_interval_seconds"],
        ) == (30, 4, 0)
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM schema_migrations WHERE version = 5")
            )
            == 1
        )
        assert await conn.scalar(text("SELECT count(*) FROM upstream_requests")) == 0
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


async def test_route_upgrade_preserves_pool_once():
    # schema_migrations is a raw SQL table, outside Base.metadata used by the
    # fixture. Explicitly emulate the pre-routing migration version.
    await initialize_database()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM schema_migrations WHERE version = 7"))
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models import UpstreamCredential, User, UserUpstreamRoute

    async with AsyncSessionLocal() as db:
        user = User(name="old-user", routing_configured=False)
        account = UpstreamCredential(provider="codex", access_token="private")
        db.add_all([user, account])
        await db.commit()
        uid, cid = user.id, account.id
    await initialize_database()
    async with AsyncSessionLocal() as db:
        routes = (await db.scalars(select(UserUpstreamRoute))).all()
        assert [(r.user_id, r.upstream_credential_id) for r in routes] == [(uid, cid)]
        assert (await db.get(User, uid)).routing_configured
        db.add(UpstreamCredential(provider="codex", access_token="new-private"))
        await db.commit()
    await initialize_database()
    async with AsyncSessionLocal() as db:
        routes = (await db.scalars(select(UserUpstreamRoute))).all()
        assert [(r.user_id, r.upstream_credential_id) for r in routes] == [(uid, cid)]


async def test_upgrade_adds_user_limits_and_optional_upstream_identity(key):
    from app.db import AsyncSessionLocal
    from app.models import User

    async with engine.begin() as conn:
        for field in ("max_concurrency", "max_rpm", "min_interval_seconds"):
            await conn.execute(text(f"ALTER TABLE users DROP COLUMN {field}"))
        for field in ("upstream_email", "upstream_name"):
            await conn.execute(
                text(f"ALTER TABLE upstream_credentials DROP COLUMN {field}")
            )
    await initialize_database()
    await initialize_database()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, key.user_id)
        assert user.max_concurrency == 2
        assert user.max_rpm == 0
        assert user.min_interval_seconds == 0
    async with engine.connect() as conn:
        # These fields can stay NULL if the upstream did not supply identity.
        await conn.execute(
            text("SELECT upstream_email, upstream_name FROM upstream_credentials")
        )


async def test_upgrade_adds_request_ids_without_changing_historical_usage(key):
    from app.db import AsyncSessionLocal
    from app.models import UsageLog

    async with engine.begin() as conn:
        await conn.execute(
            text("DROP INDEX IF EXISTS ix_usage_logs_gateway_request_id")
        )
        await conn.execute(
            text("ALTER TABLE usage_logs DROP COLUMN gateway_request_id")
        )
        await conn.execute(text("DROP TABLE upstream_attempts"))
        for field in (
            "retry_count",
            "is_final",
            "upstream_http_status",
            "failure_category",
            "elapsed_ms",
        ):
            await conn.execute(text(f"ALTER TABLE usage_logs DROP COLUMN {field}"))
        await conn.execute(
            text(
                "INSERT INTO usage_logs (api_key_id,user_id,path,status_code,prompt_tokens,completion_tokens,total_tokens,cost_usd,created_at) VALUES (:key,:user,'/v1/responses',200,4,3,7,0.25,CURRENT_TIMESTAMP)"
            ),
            {"key": key.id, "user": key.user_id},
        )
    await initialize_database()
    await initialize_database()
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        row = await db.scalar(select(UsageLog))
        assert (
            row.gateway_request_id is None
            and row.total_tokens == 7
            and row.cost_usd == 0.25
            and row.is_final is True
            and row.retry_count is None
            and row.upstream_http_status is None
        )


async def test_attempt_diagnostics_migration_preserves_existing_chain():
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models import UpstreamAttempt

    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamAttempt(
                gateway_request_id="old-request",
                attempt=1,
                retry_count=0,
                upstream_credential_id=2,
                path="/v1/responses",
                upstream_http_status=200,
                failure_category="sse_business",
                upstream_error_code="server_is_overloaded",
                retry_decision="return",
                elapsed_ms=3606,
            )
        )
        await db.commit()
    fields = (
        "retry_reason",
        "preview_stop_reason",
        "response_headers_ms",
        "preview_elapsed_ms",
        "failure_event_ms",
        "preview_stats",
    )
    async with engine.begin() as conn:
        for field in fields:
            await conn.execute(
                text(f"ALTER TABLE upstream_attempts DROP COLUMN {field}")
            )
    await initialize_database()
    await initialize_database()
    async with AsyncSessionLocal() as db:
        row = await db.scalar(select(UpstreamAttempt))
        assert row.gateway_request_id == "old-request" and row.retry_count == 0
        assert row.upstream_error_code == "server_is_overloaded"
        assert row.elapsed_ms == 3606 and row.retry_decision == "return"
        assert all(getattr(row, field) is None for field in fields)


async def test_preview_stats_upgrade_preserves_previous_retry_diagnostics():
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models import UpstreamAttempt

    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamAttempt(
                gateway_request_id="prior-diagnostic-request",
                attempt=1,
                retry_count=0,
                path="/v1/responses",
                retry_decision="return",
                retry_reason="preview_byte_limit",
                preview_stop_reason="preview_byte_limit",
                preview_elapsed_ms=25,
                response_headers_ms=1447,
                failure_event_ms=11488,
                elapsed_ms=13040,
            )
        )
        await db.commit()
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE upstream_attempts DROP COLUMN preview_stats")
        )
    await initialize_database()
    await initialize_database()
    async with AsyncSessionLocal() as db:
        row = await db.scalar(select(UpstreamAttempt))
        assert row.preview_stats is None
        assert row.retry_reason == row.preview_stop_reason == "preview_byte_limit"
        assert row.preview_elapsed_ms == 25 and row.response_headers_ms == 1447
        assert row.failure_event_ms == 11488 and row.elapsed_ms == 13040
