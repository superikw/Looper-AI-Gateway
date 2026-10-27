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
        credential_columns = await conn.run_sync(
            lambda c: {
                col["name"] for col in inspect(c).get_columns("upstream_credentials")
            }
        )
        if "deleted_at" not in credential_columns:
            await conn.execute(
                text("ALTER TABLE upstream_credentials ADD COLUMN deleted_at DATETIME")
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
