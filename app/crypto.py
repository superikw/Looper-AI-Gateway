"""Authenticated encryption at the database boundary; key lives outside SQLite."""

import fcntl
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import settings

PREFIX = "fernet:v1:"


def cipher() -> Fernet:
    if settings.credential_encryption_key:
        return Fernet(settings.credential_encryption_key.encode())
    path = Path(settings.credential_key_file).expanduser()
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
