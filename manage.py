#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import settings
from app.db import AsyncSessionLocal, engine
from app.http_client import close_http_client
from app.migrations import initialize_database
from app.models import ApiKey, OAuthSession, UpstreamCredential, User
from app.oauth_manual import (
    active_upstream_credential_query,
    complete_oauth_session,
    logout_upstream_credentials,
    start_oauth_session,
)
from app.upstreams import list_accounts, update_account, validate_binding
from app.utils import make_api_key, mask_key, sha256_text, utcnow


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def ensure_db() -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    await initialize_database()


async def cmd_generate_admin_token(_: argparse.Namespace) -> None:
    print(secrets.token_hex(32))


async def cmd_doctor(_: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(func.count(User.id)))).scalar() or 0
        keys = (await db.execute(select(func.count(ApiKey.id)))).scalar() or 0
        oauth_sessions = (
            await db.execute(select(func.count(OAuthSession.id)))
        ).scalar() or 0
        creds = (
            await db.execute(
                select(func.count(UpstreamCredential.id)).where(
                    UpstreamCredential.deleted_at.is_(None)
                )
            )
        ).scalar() or 0
    print_json(
        {
            "app_name": settings.app_name,
            "database_url": settings.database_url,
            "upstream_base_url": settings.upstream_base_url,
            "upstream_auth_mode": settings.upstream_auth_mode,
            "admin_token_configured": settings.admin_token_is_secure,
            "oauth": {
                "authorize_url_configured": bool(settings.oauth_authorize_url),
                "token_url_configured": bool(settings.oauth_token_url),
                "client_id_configured": bool(settings.oauth_client_id),
                "redirect_uri": settings.oauth_redirect_uri,
            },
            "counts": {
                "users": users,
                "api_keys": keys,
                "oauth_sessions": oauth_sessions,
                "upstream_credentials": creds,
            },
        }
    )


async def cmd_auth_start(_: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        result = await start_oauth_session(db)
    print_json(result)
    print("\n打开上面的 auth_url 完成登录，然后执行：")
    print(
        'python manage.py auth-complete --session-id <SESSION_ID> --callback-url "<完整回调URL>"'
    )


async def cmd_auth_complete(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        result = await complete_oauth_session(
            db, args.session_id, args.callback_url, getattr(args, "name", None)
        )
    print_json(result)


async def cmd_auth_status(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        if args.session_id is not None:
            entity = await db.get(OAuthSession, args.session_id)
            if not entity:
                raise SystemExit(f"oauth session not found: {args.session_id}")
            print_json(
                {
                    "id": entity.id,
                    "provider": entity.provider,
                    "status": entity.status,
                    "state": entity.state,
                    "authorize_url": entity.authorize_url,
                    "expires_at": entity.expires_at,
                    "created_at": entity.created_at,
                    "completed_at": entity.completed_at,
                    "callback_received": bool(entity.callback_url),
                }
            )
            return

        latest = (
            (await db.execute(select(OAuthSession).order_by(OAuthSession.id.desc())))
            .scalars()
            .first()
        )
        cred = (await db.execute(active_upstream_credential_query())).scalars().first()
        print_json(
            {
                "latest_session": None
                if not latest
                else {
                    "id": latest.id,
                    "status": latest.status,
                    "created_at": latest.created_at,
                    "completed_at": latest.completed_at,
                    "expires_at": latest.expires_at,
                },
                "current_credential": None
                if not cred
                else {
                    "id": cred.id,
                    "provider": cred.provider,
                    "auth_mode": cred.auth_mode,
                    "token_type": cred.token_type,
                    "scope": cred.scope,
                    "account_id": cred.account_id,
                    "expires_at": cred.expires_at,
                    "has_refresh_token": bool(cred.refresh_token),
                    "preview": mask_key(cred.access_token),
                },
            }
        )


async def cmd_logout(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        count = await logout_upstream_credentials(
            db, getattr(args, "credential_id", None)
        )
    print_json({"ok": True, "deleted_credentials": count})


async def cmd_list_upstreams(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        print_json(await list_accounts(db, args.include_deleted))


async def cmd_update_upstream(args: argparse.Namespace) -> None:
    await ensure_db()
    changes = {"reset_cooldown": args.reset_cooldown}
    if args.name is not None:
        if not 1 <= len(args.name) <= 128:
            raise SystemExit("account name must contain 1 to 128 characters")
        changes["name"] = args.name
    if args.enabled is not None:
        changes["enabled"] = args.enabled
    async with AsyncSessionLocal() as db:
        await update_account(db, args.credential_id, changes)
    print_json({"ok": True, "id": args.credential_id})


async def cmd_bind_key(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        entity = await db.get(ApiKey, args.key_id)
        if not entity or entity.deleted_at is not None:
            raise SystemExit(f"key not found: {args.key_id}")
        await validate_binding(db, args.upstream_id)
        entity.upstream_credential_id = args.upstream_id
        await db.commit()
    print_json(
        {"ok": True, "id": args.key_id, "upstream_credential_id": args.upstream_id}
    )


async def cmd_create_user(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(select(User).where(User.name == args.name))
        ).scalar_one_or_none()
        if existing:
            raise SystemExit(f"user already exists: {args.name}")
        user = User(name=args.name, note=args.note)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        print_json(
            {
                "id": user.id,
                "name": user.name,
                "note": user.note,
                "created_at": user.created_at,
            }
        )


async def cmd_list_users(_: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User).order_by(User.id.asc()))).scalars().all()
    print_json(
        [
            {"id": u.id, "name": u.name, "note": u.note, "created_at": u.created_at}
            for u in users
        ]
    )


async def cmd_create_key(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, args.user_id)
        if not user:
            raise SystemExit(f"user not found: {args.user_id}")
        raw_key = make_api_key()
        upstream_id = getattr(args, "upstream_id", None)
        await validate_binding(db, upstream_id)
        entity = ApiKey(
            user_id=args.user_id,
            name=args.name,
            key_hash=sha256_text(raw_key),
            key_preview=mask_key(raw_key) or "***",
            enabled=True,
            upstream_credential_id=upstream_id,
        )
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    print_json(
        {
            "id": entity.id,
            "user_id": entity.user_id,
            "name": entity.name,
            "api_key": raw_key,
            "key_preview": entity.key_preview,
            "enabled": entity.enabled,
            "upstream_credential_id": entity.upstream_credential_id,
            "created_at": entity.created_at,
        }
    )
    print("\n注意：api_key 明文只会显示这一次，请保存好。")


async def cmd_list_keys(_: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        keys = (
            (
                await db.execute(
                    select(ApiKey)
                    .where(ApiKey.deleted_at.is_(None))
                    .order_by(ApiKey.id.asc())
                )
            )
            .scalars()
            .all()
        )
    print_json(
        [
            {
                "id": k.id,
                "user_id": k.user_id,
                "name": k.name,
                "key_preview": k.key_preview,
                "enabled": k.enabled,
                "upstream_credential_id": k.upstream_credential_id,
                "request_count": k.request_count,
                "prompt_tokens": k.prompt_tokens,
                "completion_tokens": k.completion_tokens,
                "total_tokens": k.total_tokens,
                "created_at": k.created_at,
            }
            for k in keys
        ]
    )


async def cmd_disable_key(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        entity = await db.get(ApiKey, args.key_id)
        if not entity or entity.deleted_at is not None:
            raise SystemExit(f"key not found: {args.key_id}")
        entity.enabled = False
        await db.commit()
        print_json({"ok": True, "id": entity.id, "enabled": entity.enabled})


async def cmd_enable_key(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        entity = await db.get(ApiKey, args.key_id)
        if not entity or entity.deleted_at is not None:
            raise SystemExit(f"key not found: {args.key_id}")
        entity.enabled = True
        await db.commit()
        print_json({"ok": True, "id": entity.id, "enabled": entity.enabled})


async def cmd_delete_key(args: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        entity = await db.get(ApiKey, args.key_id)
        if not entity or entity.deleted_at is not None:
            raise SystemExit(f"key not found: {args.key_id}")
        entity.enabled = False
        entity.deleted_at = utcnow()
        await db.commit()
        print_json({"ok": True, "deleted": args.key_id})


async def cmd_usage(_: argparse.Namespace) -> None:
    await ensure_db()
    async with AsyncSessionLocal() as db:
        keys = (
            (await db.execute(select(ApiKey).order_by(ApiKey.id.asc()))).scalars().all()
        )
    totals = {
        "request_count": sum(k.request_count for k in keys),
        "prompt_tokens": sum(k.prompt_tokens for k in keys),
        "completion_tokens": sum(k.completion_tokens for k in keys),
        "total_tokens": sum(k.total_tokens for k in keys),
    }
    items = [
        {
            "id": k.id,
            "user_id": k.user_id,
            "name": k.name,
            "key_preview": k.key_preview,
            "enabled": k.enabled,
            "request_count": k.request_count,
            "prompt_tokens": k.prompt_tokens,
            "completion_tokens": k.completion_tokens,
            "total_tokens": k.total_tokens,
        }
        for k in keys
    ]
    print_json({"items": items, "totals": totals})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local admin CLI for Looper-AI-Gateway"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Show current config and DB state").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser(
        "generate-admin-token", help="Generate a strong ADMIN_TOKEN"
    ).set_defaults(func=cmd_generate_admin_token)
    sub.add_parser(
        "auth-start", help="Start OAuth login and print auth_url"
    ).set_defaults(func=cmd_auth_start)

    p = sub.add_parser("auth-complete", help="Complete OAuth login with callback URL")
    p.add_argument("--session-id", type=int, required=True)
    p.add_argument("--callback-url", required=True)
    p.add_argument("--name", help="Optional account name")
    p.set_defaults(func=cmd_auth_complete)

    p = sub.add_parser(
        "auth-status", help="Show latest auth status or one specific session"
    )
    p.add_argument("--session-id", type=int)
    p.set_defaults(func=cmd_auth_status)

    p = sub.add_parser("logout", help="Soft-delete one or all stored upstream accounts")
    p.add_argument("--credential-id", type=int, help="Omit to log out all accounts")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("list-upstreams", help="List upstream accounts and usage")
    p.add_argument("--include-deleted", action="store_true")
    p.set_defaults(func=cmd_list_upstreams)

    p = sub.add_parser("update-upstream", help="Rename, pause or resume an account")
    p.add_argument("credential_id", type=int)
    p.add_argument("--name")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--enable", dest="enabled", action="store_const", const=True)
    group.add_argument("--disable", dest="enabled", action="store_const", const=False)
    p.add_argument("--reset-cooldown", action="store_true")
    p.set_defaults(func=cmd_update_upstream, enabled=None)

    p = sub.add_parser("create-user", help="Create a new user")
    p.add_argument("name")
    p.add_argument("--note")
    p.set_defaults(func=cmd_create_user)

    sub.add_parser("list-users", help="List users").set_defaults(func=cmd_list_users)

    p = sub.add_parser("create-key", help="Create a new API key for a user")
    p.add_argument("--user-id", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument(
        "--upstream-id", type=int, help="Bind to one account; omit for the pool"
    )
    p.set_defaults(func=cmd_create_key)

    sub.add_parser("list-keys", help="List API keys").set_defaults(func=cmd_list_keys)

    p = sub.add_parser(
        "bind-key", help="Bind a key to one account or return to the pool"
    )
    p.add_argument("key_id", type=int)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--upstream-id", type=int)
    group.add_argument("--auto", dest="upstream_id", action="store_const", const=None)
    p.set_defaults(func=cmd_bind_key)

    p = sub.add_parser("disable-key", help="Disable an API key")
    p.add_argument("key_id", type=int)
    p.set_defaults(func=cmd_disable_key)

    p = sub.add_parser("enable-key", help="Enable an API key")
    p.add_argument("key_id", type=int)
    p.set_defaults(func=cmd_enable_key)

    p = sub.add_parser("delete-key", help="Delete an API key")
    p.add_argument("key_id", type=int)
    p.set_defaults(func=cmd_delete_key)

    sub.add_parser("usage", help="Show aggregated usage").set_defaults(func=cmd_usage)
    return parser


async def amain() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        await args.func(args)
    except HTTPException as exc:
        raise SystemExit(f"{exc.status_code}: {exc.detail}") from None
    finally:
        await close_http_client()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(amain())
