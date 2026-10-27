from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Optional


def utcnow() -> datetime:
    """Naive UTC preserves compatibility with existing SQLite DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def make_api_key(prefix: str = "gtw") -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mask_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"
