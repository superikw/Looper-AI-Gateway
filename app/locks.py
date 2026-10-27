"""Advisory process locks for the single-host SQLite deployment."""

import asyncio
import fcntl
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from app.config import settings


def lock_path(name: str) -> Path:
    folder = Path(settings.credential_key_file).expanduser().parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(settings.database_url.encode()).hexdigest()[:16]
    return folder / f"{digest}-{name}.lock"


def try_process_lock(name: str) -> int | None:
    fd = os.open(lock_path(name), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise


@asynccontextmanager
async def database_lock(name: str, timeout: float = 120):
    fd = os.open(lock_path(name), os.O_CREAT | os.O_RDWR, 0o600)
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
