"""Authenticated encryption at the database boundary; key lives outside SQLite."""

import fcntl
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import settings

PREFIX = "fernet:v1:"
_cache_lock = Lock()


def cipher() -> Fernet:
    key = settings.credential_encryption_key
    path = (
        "" if key else os.path.abspath(os.path.expanduser(settings.credential_key_file))
    )
    # Serialize cache misses as well as file initialization across threads.
    # PID prevents a child process from reusing its parent's cached instance.
    with _cache_lock:
        return _load_cipher(key, path, os.getpid())


@lru_cache(maxsize=1)
def _load_cipher(key: str, key_file: str, process_id: int) -> Fernet:
    if key:
        return Fernet(key.encode())
    path = Path(key_file)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        key = stream.read().strip()
        if not key:
            key = Fernet.generate_key()
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    return Fernet(key)


def _after_fork() -> None:
    global _cache_lock
    # A different thread might have held the lock when the parent forked.
    _cache_lock = Lock()


os.register_at_fork(after_in_child=_after_fork)


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return PREFIX + cipher().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None or not value.startswith(PREFIX):
        return value  # Legacy rows are rewritten by initialize_database.
    try:
        return cipher().decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        raise ValueError(
            "Cannot decrypt credentials; restore the original credential encryption key"
        ) from None


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
