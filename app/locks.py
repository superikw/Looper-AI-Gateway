"""Advisory process locks for the single-host SQLite deployment."""

import asyncio
import fcntl
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from app.config import settings


@asynccontextmanager
async def database_lock(name: str, timeout: float = 120):
    folder = Path(settings.credential_key_file).expanduser().parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(settings.database_url.encode()).hexdigest()[:16]
    fd = os.open(folder / f"{digest}-{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.02)
        yield
    finally:
        os.close(fd)
