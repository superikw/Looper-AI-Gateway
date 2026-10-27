import json

from sqlalchemy import select

import manage
from app.db import AsyncSessionLocal
from app.models import ApiKey, UpstreamCredential


async def run(command):
    args = manage.build_parser().parse_args(command)
    await args.func(args)


async def test_cli_manages_accounts_and_binding(key, capsys):
    async with AsyncSessionLocal() as db:
        rows = [
            UpstreamCredential(
                provider="codex", account_id=f"cli-{i}", access_token=f"secret-{i}"
            )
            for i in range(2)
        ]
        db.add_all(rows)
        await db.commit()
        first, second = [row.id for row in rows]
    await run(["list-upstreams"])
    listed = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in listed] == [first, second]
    assert "secret" not in str(listed)
    await run(["update-upstream", str(first), "--disable", "--name", "Paused"])
    await run(["bind-key", str(key.id), "--upstream-id", str(first)])
    await run(
        [
            "create-key",
            "--user-id",
            str(key.user_id),
            "--name",
            "bound",
            "--upstream-id",
            str(second),
        ]
    )
    async with AsyncSessionLocal() as db:
        assert (await db.get(ApiKey, key.id)).upstream_credential_id == first
        new = await db.scalar(select(ApiKey).where(ApiKey.name == "bound"))
        assert new.upstream_credential_id == second
        account = await db.get(UpstreamCredential, first)
        assert account.name == "Paused" and not account.enabled
    await run(["bind-key", str(key.id), "--auto"])
    await run(["logout", "--credential-id", str(first)])
    async with AsyncSessionLocal() as db:
        assert (await db.get(ApiKey, key.id)).upstream_credential_id is None
        assert (await db.get(UpstreamCredential, first)).deleted_at is not None
        assert (await db.get(UpstreamCredential, second)).deleted_at is None
