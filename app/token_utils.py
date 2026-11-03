"""Read routing metadata from JWT claims; this does not verify signatures."""

import base64
import json
from typing import Any

JWT_CLAIM_PATH = "https://api.openai.com/auth"


def _decode_jwt_payload(access_token: str) -> dict[str, Any] | None:
    if not isinstance(access_token, str):
        return None
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        obj = json.loads(decoded)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def extract_account_id_from_token(access_token: str) -> str | None:
    payload = _decode_jwt_payload(access_token)
    if not payload:
        return None
    auth_claim = payload.get(JWT_CLAIM_PATH)
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    return None


def extract_identity(token_data: dict[str, Any]) -> dict[str, str | None]:
    """Optional display metadata only; never used for authentication."""
    sources = [token_data]
    for field in ("id_token", "access_token"):
        payload = _decode_jwt_payload(token_data.get(field))
        if payload:
            sources.append(payload)
            profile = payload.get("https://api.openai.com/profile")
            if isinstance(profile, dict):
                sources.append(profile)

    def first(key):
        for source in sources:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:255]
        return None

    return {"upstream_email": first("email"), "upstream_name": first("name")}


def account_display_name(name, email, upstream_name, account_id, local_id) -> str:
    return next(
        (
            value.strip()
            for value in (name, email, upstream_name, account_id)
            if isinstance(value, str) and value.strip()
        ),
        f"账户 #{local_id}",
    )
