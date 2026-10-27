"""Small, versioned migrations shared by the server and local CLI."""

from sqlalchemy import inspect, text

from app.crypto import PREFIX, cipher, decrypt, encrypt
from app.db import Base, engine
from app.locks import database_lock


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
            rows = (
                (
                    await conn.execute(
                        text(f"SELECT id, {', '.join(fields)} FROM {table}")
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                for field in fields:
                    value = row[field]
                    if value is not None and not value.startswith(PREFIX):
                        await conn.execute(
                            text(f"UPDATE {table} SET {field} = :value WHERE id = :id"),
                            {"id": row["id"], "value": encrypt(value)},
                        )
                    elif value is not None:
                        decrypt(
                            value
                        )  # Fail before serving if the encryption key is wrong.
        await conn.execute(
            text(
                "UPDATE oauth_sessions SET callback_url = 'received', code_verifier = :empty WHERE status != 'pending' AND callback_url IS NOT NULL"
            ),
            {"empty": encrypt("")},
        )
        if not (
            await conn.execute(
                text("SELECT version FROM schema_migrations WHERE version = 2")
            )
        ).first():
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (2)")
            )
