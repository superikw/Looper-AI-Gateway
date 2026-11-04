"""Small, versioned migrations shared by the server and local CLI."""

from sqlalchemy import inspect, text

from app.crypto import PREFIX, cipher, decrypt, encrypt
from app.db import Base, engine
from app.locks import database_lock

MIGRATION_BATCH_SIZE = 256


async def migrate_encrypted_rows(conn, table: str, fields: list[str]) -> None:
    """Encrypt in Python, then flush one executemany UPDATE per changed batch."""
    columns = list(fields)
    if table == "oauth_sessions":
        columns += ["status", "callback_url"]
    assignments = list(fields)
    if table == "oauth_sessions":
        assignments.append("callback_url")
    update = text(
        f"UPDATE {table} SET "
        + ", ".join(f"{field} = :{field}" for field in assignments)
        + " WHERE id = :id"
    )
    last_id = None
    while True:
        where = "WHERE id > :last_id" if last_id is not None else ""
        rows = (
            (
                await conn.execute(
                    text(
                        f"SELECT id, {', '.join(columns)} FROM {table} {where} ORDER BY id LIMIT :batch_size"
                    ),
                    {"last_id": last_id, "batch_size": MIGRATION_BATCH_SIZE},
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        updates = []
        for row in rows:
            values = {field: row[field] for field in assignments}
            plaintext = {}
            for field in fields:
                value = row[field]
                # Verify encrypted rows too so a wrong key still fails startup.
                plaintext[field] = decrypt(value)
                if value is not None and not value.startswith(PREFIX):
                    values[field] = encrypt(value)
            if (
                table == "oauth_sessions"
                and row["status"] != "pending"
                and row["callback_url"] is not None
            ):
                values["callback_url"] = "received"
                if plaintext["code_verifier"] != "":
                    values["code_verifier"] = encrypt("")
            if any(values[field] != row[field] for field in assignments):
                updates.append({"id": row["id"], **values})
        if updates:
            await conn.execute(update, updates)
        last_id = rows[-1]["id"]


async def initialize_database() -> None:
    cipher()
    async with database_lock("migration"), engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
            )
        )
        columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("api_keys")}
        )
        if "deleted_at" not in columns:
            await conn.execute(
                text("ALTER TABLE api_keys ADD COLUMN deleted_at DATETIME")
            )
        user_columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("users")}
        )
        for field, definition in {
            "max_concurrency": "INTEGER NOT NULL DEFAULT 2",
            "max_rpm": "INTEGER NOT NULL DEFAULT 0",
            "min_interval_seconds": "FLOAT NOT NULL DEFAULT 0",
        }.items():
            if field not in user_columns:
                await conn.execute(
                    text(f"ALTER TABLE users ADD COLUMN {field} {definition}")
                )
        if "routing_configured" not in user_columns:
            await conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN routing_configured BOOLEAN NOT NULL DEFAULT 0"
                )
            )
        if "enabled" not in user_columns:
            await conn.execute(
                text("ALTER TABLE users ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1")
            )
        if "deleted_at" not in user_columns:
            await conn.execute(text("ALTER TABLE users ADD COLUMN deleted_at DATETIME"))
        credential_columns = await conn.run_sync(
            lambda c: {
                col["name"] for col in inspect(c).get_columns("upstream_credentials")
            }
        )
        if "deleted_at" not in credential_columns:
            await conn.execute(
                text("ALTER TABLE upstream_credentials ADD COLUMN deleted_at DATETIME")
            )
        additions = {
            "upstream_email": "VARCHAR(255)",
            "upstream_name": "VARCHAR(255)",
            "name": "VARCHAR(128)",
            "enabled": "BOOLEAN NOT NULL DEFAULT 1",
            "max_rpm": "INTEGER NOT NULL DEFAULT 30",
            "max_concurrency": "INTEGER NOT NULL DEFAULT 4",
            "min_interval_seconds": "FLOAT NOT NULL DEFAULT 0",
            "cooldown_until": "DATETIME",
            "last_used_at": "DATETIME",
            "last_dispatched_at": "DATETIME",
            "request_count": "INTEGER NOT NULL DEFAULT 0",
            "failure_count": "INTEGER NOT NULL DEFAULT 0",
            "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
            "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
            "total_tokens": "INTEGER NOT NULL DEFAULT 0",
        }
        for field, sql_type in additions.items():
            if field not in credential_columns:
                await conn.execute(
                    text(
                        f"ALTER TABLE upstream_credentials ADD COLUMN {field} {sql_type}"
                    )
                )
        for table in ("api_keys", "usage_logs"):
            current_columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns(table)}
            )
            if "upstream_credential_id" not in current_columns:
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} ADD COLUMN upstream_credential_id INTEGER REFERENCES upstream_credentials(id)"
                    )
                )
        usage_columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("usage_logs")}
        )
        for field, definition in {
            "retry_count": "INTEGER",
            "is_final": "BOOLEAN NOT NULL DEFAULT 1",
            "upstream_http_status": "INTEGER",
            "failure_category": "VARCHAR(64)",
            "elapsed_ms": "INTEGER",
        }.items():
            if field not in usage_columns:
                await conn.execute(
                    text(f"ALTER TABLE usage_logs ADD COLUMN {field} {definition}")
                )
        attempt_columns = await conn.run_sync(
            lambda c: {
                col["name"] for col in inspect(c).get_columns("upstream_attempts")
            }
        )
        if "retry_count" not in attempt_columns:
            await conn.execute(
                text("ALTER TABLE upstream_attempts ADD COLUMN retry_count INTEGER")
            )
        if "gateway_request_id" not in usage_columns:
            await conn.execute(
                text("ALTER TABLE usage_logs ADD COLUMN gateway_request_id VARCHAR(64)")
            )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_usage_logs_gateway_request_id ON usage_logs (gateway_request_id)"
            )
        )
        if "cost_usd" not in usage_columns:
            await conn.execute(
                text(
                    "ALTER TABLE usage_logs ADD COLUMN cost_usd FLOAT NOT NULL DEFAULT 0"
                )
            )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_usage_logs_upstream_credential_id ON usage_logs (upstream_credential_id)"
            )
        )
        await conn.execute(
            text("INSERT OR IGNORE INTO schema_migrations (version) VALUES (5)")
        )
        await conn.execute(
            text("INSERT OR IGNORE INTO schema_migrations (version) VALUES (6)")
        )
        if not (
            await conn.execute(
                text("SELECT version FROM schema_migrations WHERE version = 7")
            )
        ).first():
            # Preserve the existing pool behavior once, at upgrade time. Future
            # users/accounts start with no assignments and require explicit routing.
            await conn.execute(
                text("""
                INSERT OR IGNORE INTO user_upstream_routes (user_id, upstream_credential_id)
                SELECT u.id, c.id FROM users u CROSS JOIN upstream_credentials c
                WHERE u.deleted_at IS NULL AND c.deleted_at IS NULL AND c.provider = 'codex'
            """)
            )
            if (
                await conn.execute(
                    text("SELECT id FROM users WHERE routing_configured = 0 LIMIT 1")
                )
            ).first():
                await conn.execute(
                    text(
                        "UPDATE users SET routing_configured = 1 WHERE routing_configured = 0"
                    )
                )
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (7)")
            )
        if not (
            await conn.execute(
                text("SELECT version FROM schema_migrations WHERE version = 4")
            )
        ).first():
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (4)")
            )
        if not (
            await conn.execute(
                text("SELECT version FROM schema_migrations WHERE version = 3")
            )
        ).first():
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (3)")
            )
        if not (
            await conn.execute(
                text("SELECT version FROM schema_migrations WHERE version = 1")
            )
        ).first():
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (1)")
            )
        # Scan on every startup too: older CLI versions may have written plaintext.
        for table, fields in [
            ("upstream_credentials", ["access_token", "refresh_token", "raw_json"]),
            ("oauth_sessions", ["code_verifier"]),
        ]:
            await migrate_encrypted_rows(conn, table, fields)
        if not (
            await conn.execute(
                text("SELECT version FROM schema_migrations WHERE version = 2")
            )
        ).first():
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (2)")
            )
