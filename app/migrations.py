"""Small, versioned migrations shared by the server and local CLI."""
from sqlalchemy import inspect, text

from app.db import Base, engine


async def initialize_database() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)'))
        columns = await conn.run_sync(lambda c: {col['name'] for col in inspect(c).get_columns('api_keys')})
        if 'deleted_at' not in columns:
            await conn.execute(text('ALTER TABLE api_keys ADD COLUMN deleted_at DATETIME'))
        if not (await conn.execute(text('SELECT version FROM schema_migrations WHERE version = 1'))).first():
            await conn.execute(text('INSERT INTO schema_migrations (version) VALUES (1)'))
