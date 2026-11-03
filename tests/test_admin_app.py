import httpx
import pytest

from app.admin_app import admin_app
from app.admin_service import hash_password
from app.db import AsyncSessionLocal
from app.models import AdminAccount, UsageLog


@pytest.fixture
async def admin_client():
    async with AsyncSessionLocal() as db:
        db.add(
            AdminAccount(
                username="root",
                password_hash=hash_password("root-password-123"),
                is_superadmin=True,
                must_change_password=False,
            )
        )
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="https://admin"
    ) as client:
        yield client


async def test_admin_login_and_user_provision(admin_client):
    response = await admin_client.post(
        "/api/login",
        json={"username": "root", "password": "root-password-123"},
    )
    assert response.status_code == 200
    created = await admin_client.post("/api/users", json={"name": "alice"})
    assert created.status_code == 200
    assert created.json()["api_key"].startswith("gtw_")
    assert len((await admin_client.get("/api/users")).json()) == 1


async def test_admin_password_reset_revokes_sessions(admin_client):
    await admin_client.post(
        "/api/login",
        json={"username": "root", "password": "root-password-123"},
    )
    created = await admin_client.post("/api/admins", json={"username": "operator"})
    admin_id = created.json()["id"]
    reset = await admin_client.post(f"/api/admins/{admin_id}/reset-password")
    assert reset.status_code == 200
    assert reset.json()["initial_password"]


async def test_dashboard_calculates_cost(admin_client, key):
    await admin_client.post(
        "/api/login",
        json={"username": "root", "password": "root-password-123"},
    )
    async with AsyncSessionLocal() as db:
        db.add(
            UsageLog(
                api_key_id=key.id,
                user_id=key.user_id,
                path="/v1/responses",
                model="gpt-test",
                status_code=200,
                total_tokens=10,
                cost_usd=0.25,
            )
        )
        await db.commit()
    dashboard = (await admin_client.get("/api/dashboard")).json()
    assert dashboard["periods"]["day"]["tokens"] == 10
    assert dashboard["periods"]["day"]["cost_usd"] == 0.25


async def sign_in(client):
    response = await client.post(
        "/api/login", json={"username": "root", "password": "root-password-123"}
    )
    assert response.status_code == 200
    return response


async def test_login_security_and_separate_pages(admin_client):
    response = await sign_in(admin_client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie
    for path in (
        "/",
        "/dashboard",
        "/users",
        "/logs",
        "/prices",
        "/settings",
        "/upstreams",
    ):
        response = await admin_client.get(path)
        assert response.status_code == 200
        assert 'method="post"' in response.text
        assert 'type="password"' in response.text
        assert "onsubmit=" not in response.text
        assert "script-src 'self'" in response.headers["content-security-policy"]
    assert (await admin_client.get("/api/login?password=secret")).status_code == 400
    assert (await admin_client.get("/api/login")).status_code == 405
    response = await admin_client.put(
        "/api/settings",
        json={"timezone": "UTC"},
        headers={"Origin": "https://evil.invalid"},
    )
    assert response.status_code == 403
    assert (await admin_client.get("/static/admin.js")).status_code == 200


async def test_remote_http_is_rejected_when_https_required(admin_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_require_https", True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as client:
        response = await client.post(
            "/api/login", json={"username": "root", "password": "root-password-123"}
        )
        assert response.status_code == 426
        assert "admin_session" not in response.cookies


async def test_timestamps_have_utc_offset_and_settings_apply(admin_client, key):
    from datetime import datetime

    from app.models import ActiveSession

    await sign_in(admin_client)
    async with AsyncSessionLocal() as db:
        db.add(
            ActiveSession(
                id="active-test",
                user_id=key.user_id,
                api_key_id=key.id,
                path="/v1/responses",
                model="test",
                started_at=datetime(2026, 1, 1, 0, 0),
            )
        )
        db.add(
            UsageLog(
                user_id=key.user_id,
                api_key_id=key.id,
                path="/v1/responses",
                status_code=200,
                created_at=datetime(2026, 1, 1, 0, 0),
            )
        )
        await db.commit()
    response = await admin_client.put(
        "/api/settings", json={"timezone": "Asia/Shanghai"}
    )
    assert response.json()["timezone"] == "Asia/Shanghai"
    logs = (await admin_client.get("/api/logs")).json()
    sessions = (await admin_client.get("/api/sessions")).json()
    assert logs[0]["created_at"] == "2026-01-01T00:00:00Z"
    assert sessions[0]["started_at"] == "2026-01-01T00:00:00Z"
    assert (
        await admin_client.put("/api/settings", json={"timezone": "invalid/timezone"})
    ).status_code == 400


async def test_bidirectional_routes_and_empty_assignment(admin_client, key):
    from app.models import UpstreamCredential
    from app.upstreams import claim_account

    await sign_in(admin_client)
    async with AsyncSessionLocal() as db:
        a = UpstreamCredential(provider="codex", name="A", access_token="secret-a")
        b = UpstreamCredential(provider="codex", name="B", access_token="secret-b")
        db.add_all([a, b])
        await db.commit()
        first, second = a.id, b.id
    created = (await admin_client.post("/api/users", json={"name": "other"})).json()
    uid = created["id"]
    assert await claim_account(None, set(), uid) is None
    assert (
        await admin_client.put(
            f"/api/users/{uid}/upstreams", json={"upstream_ids": [first, second, first]}
        )
    ).status_code == 200
    users = (await admin_client.get("/api/users")).json()
    assert sorted(next(u for u in users if u["id"] == uid)["upstream_ids"]) == [
        first,
        second,
    ]
    assert (
        await admin_client.put(
            f"/api/upstreams/{first}/users", json={"user_ids": [uid, key.user_id]}
        )
    ).status_code == 200
    lease = await claim_account(None, set(), uid)
    assert lease.credential_id in {first, second}
    await lease.release()
    # Narrowing by a key must never escape assignments.
    await admin_client.put(
        f"/api/users/{uid}/upstreams", json={"upstream_ids": [second]}
    )
    assert await claim_account(first, set(), uid) is None
    # Failed assignment leaves the old routes intact.
    assert (
        await admin_client.put(
            f"/api/users/{uid}/upstreams", json={"upstream_ids": [99999]}
        )
    ).status_code == 404
    lease = await claim_account(None, set(), uid)
    assert lease.credential_id == second
    await lease.release()
    await admin_client.put(f"/api/users/{uid}/upstreams", json={"upstream_ids": []})
    assert await claim_account(None, set(), uid) is None
    pool = (await admin_client.get("/api/upstreams")).json()["accounts"]
    assert next(a for a in pool if a["id"] == second)["user_ids"] == []
    assert "secret-a" not in str(pool)


async def test_new_user_no_route_rejects_real_proxy_request(
    admin_client, client, monkeypatch
):
    from app.config import settings
    from app.models import UpstreamCredential

    monkeypatch.setattr(settings, "upstream_auth_mode", "oauth_manual")
    await sign_in(admin_client)
    async with AsyncSessionLocal() as db:
        db.add(
            UpstreamCredential(
                provider="codex", access_token="secret", account_id="account"
            )
        )
        await db.commit()
    user = (await admin_client.post("/api/users", json={"name": "unassigned"})).json()
    response = await client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer " + user["api_key"]},
        json={"model": "gpt-test", "input": "hello"},
    )
    assert response.status_code == 503


async def test_upstream_maintenance_and_quota(admin_client, monkeypatch):
    from app.models import UpstreamCredential

    await sign_in(admin_client)
    async with AsyncSessionLocal() as db:
        account = UpstreamCredential(
            provider="codex", access_token="secret-token", account_id="account"
        )
        db.add(account)
        await db.commit()
        cid = account.id

    class QuotaClient:
        async def get(self, url, **kwargs):
            assert url.endswith("/wham/usage")
            assert kwargs["headers"]["Authorization"] == "Bearer secret-token"
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 25,
                            "reset_at": 1800000000,
                            "limit_window_seconds": 18000,
                        }
                    },
                },
            )

    monkeypatch.setattr("app.admin_upstreams.get_http_client", lambda: QuotaClient())
    quota = await admin_client.post(f"/api/upstreams/{cid}/usage/refresh")
    assert quota.status_code == 200
    assert quota.json()["primary"]["used_percent"] == 25
    assert quota.json()["primary"]["reset_at"].endswith("+00:00")
    pool = await admin_client.get("/api/upstreams")
    assert pool.json()["accounts"][0]["quota"]["plan_type"] == "plus"
    assert "secret-token" not in pool.text
    assert (
        await admin_client.patch(
            f"/api/upstreams/{cid}",
            json={"enabled": False, "max_rpm": 5, "max_concurrency": 2},
        )
    ).status_code == 200
    assert (await admin_client.get("/api/upstreams")).json()["accounts"][0][
        "status"
    ] == "disabled"
    assert (await admin_client.delete(f"/api/upstreams/{cid}")).status_code == 200
    assert (await admin_client.get("/api/upstreams")).json()["accounts"] == []


async def test_upstream_add_oauth(admin_client, monkeypatch):
    await sign_in(admin_client)

    async def exchange(code, verifier):
        return {
            "account_id": "new-account",
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr("app.oauth_manual.exchange_code_for_token", exchange)
    start = (await admin_client.post("/api/upstreams/oauth/start")).json()
    response = await admin_client.post(
        "/api/upstreams/oauth/complete",
        json={
            "session_id": start["session_id"],
            "name": "Added",
            "callback_url": f"http://localhost:1455/auth/callback?code=test&state={start['state']}",
        },
    )
    assert response.status_code == 200
    assert "private-" not in response.text
    account = (await admin_client.get("/api/upstreams")).json()["accounts"][0]
    assert account["name"] == "Added" and account["user_ids"] == []


@pytest.mark.parametrize("stream", [False, True])
async def test_assigned_routes_control_real_proxy_dispatch(
    admin_client, client, monkeypatch, stream
):
    import json

    from app.config import settings
    from app.models import UpstreamCredential

    monkeypatch.setattr(settings, "upstream_auth_mode", "oauth_manual")
    await sign_in(admin_client)
    async with AsyncSessionLocal() as db:
        first = UpstreamCredential(
            provider="codex", account_id="unassigned-account", access_token="private-a"
        )
        second = UpstreamCredential(
            provider="codex", account_id="assigned-account", access_token="private-b"
        )
        db.add_all([first, second])
        await db.commit()
        cid = second.id
    created = (await admin_client.post("/api/users", json={"name": "routed"})).json()
    await admin_client.put(
        f"/api/users/{created['id']}/upstreams", json={"upstream_ids": [cid]}
    )
    dispatched = []

    def handle(request):
        dispatched.append(request.headers["chatgpt-account-id"])
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp",
                        "status": "completed",
                        "output": [],
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                }
            )
            + "\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        monkeypatch.setattr("app.services.get_http_client", lambda: upstream)
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + created["api_key"]},
            json={"model": "gpt-test", "input": "hello", "stream": stream},
        )
    assert response.status_code == 200, response.text
    assert dispatched == ["assigned-account"]


async def test_login_attempts_are_limited(admin_client):
    from app.admin_app import _login_failures

    try:
        for _ in range(5):
            response = await admin_client.post(
                "/api/login", json={"username": "root", "password": "incorrect"}
            )
            assert response.status_code == 401
        response = await admin_client.post(
            "/api/login", json={"username": "root", "password": "incorrect"}
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
    finally:
        _login_failures.clear()


async def test_remote_http_login_allowed_by_default(admin_client, monkeypatch):
    from app.config import Settings, settings

    assert Settings(_env_file=None).admin_require_https is False
    monkeypatch.setattr(settings, "admin_require_https", False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin"
    ) as client:
        html = await client.get("/")
        assert 'data-require-https="false"' in html.text
        response = await client.post(
            "/api/login", json={"username": "root", "password": "root-password-123"}
        )
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" not in response.headers["set-cookie"]
        assert (await client.get("/api/me")).status_code == 200


async def test_log_api_includes_original_interface(admin_client, key):
    await sign_in(admin_client)
    async with AsyncSessionLocal() as db:
        for path in ("/v1/responses", "/v1/chat/completions"):
            db.add(
                UsageLog(
                    user_id=key.user_id,
                    api_key_id=key.id,
                    path=path,
                    model="gpt-test",
                    status_code=200,
                )
            )
        await db.commit()
    logs = (await admin_client.get("/api/logs")).json()
    assert {log["path"] for log in logs} == {"/v1/responses", "/v1/chat/completions"}


@pytest.mark.parametrize(
    "path", ["/v1/responses", "/v1/chat/completions", "/v1/embeddings"]
)
async def test_live_sessions_and_recently_finished_requests(
    admin_client, client, key, monkeypatch, path
):
    import asyncio
    from datetime import timedelta

    from app.models import ActiveSession
    from app.utils import utcnow

    await sign_in(admin_client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def forward(*args, **kwargs):
        entered.set()
        await release.wait()
        if path == "/v1/embeddings":
            return 200, b"{}", None, None, "application/json"
        return 200, {}, None, None

    monkeypatch.setattr("app.main.forward_to_upstream", forward)
    monkeypatch.setattr("app.main.forward_raw_to_upstream", forward)
    payload = (
        {"model": "gpt-test", "input": "hello"}
        if path != "/v1/chat/completions"
        else {"model": "gpt-test", "messages": [{"role": "user", "content": "hello"}]}
    )
    pending = asyncio.create_task(
        client.post(path, headers={"Authorization": "Bearer gtw_test"}, json=payload)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        live = (await admin_client.get("/api/sessions")).json()
        assert len(live) == 1 and live[0]["path"] == path
        assert live[0]["status"] == "running" and live[0]["finished_at"] is None
    finally:
        release.set()
        response = await asyncio.wait_for(pending, 2)
    assert response.status_code == 200
    assert (await admin_client.get("/api/sessions")).json() == []
    recent = (await admin_client.get("/api/sessions?recent_seconds=300")).json()
    assert len(recent) == 1 and recent[0]["status"] == "completed"
    assert recent[0]["finished_at"] is not None
    async with AsyncSessionLocal() as db:
        row = await db.get(ActiveSession, recent[0]["id"])
        row.finished_at = utcnow() - timedelta(minutes=6)
        await db.commit()
    assert (await admin_client.get("/api/sessions?recent_seconds=300")).json() == []
    assert (
        await admin_client.get("/api/sessions?recent_seconds=-1")
    ).status_code == 422


async def test_identity_fallback_and_upstream_in_logs(admin_client, key):
    from app.models import UpstreamCredential

    await sign_in(admin_client)
    async with AsyncSessionLocal() as db:
        account = UpstreamCredential(
            provider="codex",
            name="我的备注",
            upstream_email="owner@example.com",
            upstream_name="Owner",
            account_id="account-id",
            access_token="secret",
        )
        db.add(account)
        await db.flush()
        cid = account.id
        db.add(
            UsageLog(
                api_key_id=key.id,
                user_id=key.user_id,
                path="/v1/responses",
                model="gpt-test",
                status_code=200,
                upstream_credential_id=cid,
            )
        )
        db.add(
            UsageLog(
                api_key_id=key.id,
                user_id=key.user_id,
                path="/v1/responses",
                status_code=429,
            )
        )
        await db.commit()
    pool = (await admin_client.get("/api/upstreams")).json()["accounts"]
    assert pool[0]["display_name"] == "我的备注"
    response = await admin_client.patch(f"/api/upstreams/{cid}", json={"name": ""})
    assert response.status_code == 200, response.text
    assert (await admin_client.get("/api/upstreams")).json()["accounts"][0][
        "display_name"
    ] == "owner@example.com"
    logs = (await admin_client.get("/api/logs")).json()
    routed = next(log for log in logs if log["upstream_credential_id"] == cid)
    assert routed["upstream_display_name"] == "owner@example.com"
    assert routed["upstream_account_id"] == "account-id"
    assert logs[0]["upstream_display_name"] is None
    await admin_client.delete(f"/api/upstreams/{cid}")
    assert len((await admin_client.get("/api/logs")).json()) == 2


async def test_user_limit_settings(admin_client, key):
    await sign_in(admin_client)
    users = (await admin_client.get("/api/users")).json()
    assert users[0]["max_concurrency"] == 2
    response = await admin_client.patch(
        f"/api/users/{key.user_id}/limits",
        json={"max_rpm": 10, "min_interval_seconds": 0.5},
    )
    assert response.status_code == 200
    user = (await admin_client.get("/api/users")).json()[0]
    assert (
        user["max_rpm"] == 10
        and user["max_concurrency"] == 2
        and user["min_interval_seconds"] == 0.5
    )
    assert (
        await admin_client.patch(
            f"/api/users/{key.user_id}/limits", json={"max_concurrency": 0}
        )
    ).status_code == 422


async def test_oauth_saves_optional_upstream_identity(
    admin_client, monkeypatch, make_jwt
):
    await sign_in(admin_client)

    async def exchange(*args):
        return {
            "access_token": "opaque-access",
            "refresh_token": "opaque-refresh",
            "account_id": "identity-account",
            "id_token": make_jwt(
                {"email": "identity@example.com", "name": "Identity Name"}
            ),
            "expires_in": 3600,
        }

    monkeypatch.setattr("app.oauth_manual.exchange_code_for_token", exchange)
    start = (await admin_client.post("/api/upstreams/oauth/start")).json()
    completed = await admin_client.post(
        "/api/upstreams/oauth/complete",
        json={
            "session_id": start["session_id"],
            "callback_url": f"http://localhost:1455/auth/callback?code=x&state={start['state']}",
        },
    )
    assert completed.status_code == 200, completed.text
    account = (await admin_client.get("/api/upstreams")).json()["accounts"][0]
    assert account["upstream_email"] == "identity@example.com"
    assert account["upstream_name"] == "Identity Name"
    assert account["display_name"] == "identity@example.com"


async def test_admin_can_read_attempt_chain_and_old_logs_still_work(admin_client, key):
    from app.models import UpstreamAttempt

    await sign_in(admin_client)
    async with AsyncSessionLocal() as db:
        log = UsageLog(
            user_id=key.user_id,
            api_key_id=key.id,
            path="/v1/responses",
            status_code=502,
            gateway_request_id="gw_test",
        )
        old = UsageLog(
            user_id=key.user_id,
            api_key_id=key.id,
            path="/v1/responses",
            status_code=200,
        )
        db.add_all([log, old])
        await db.flush()
        log_id, old_id = log.id, old.id
        db.add(
            UpstreamAttempt(
                gateway_request_id="gw_test",
                attempt=1,
                path="/v1/responses",
                upstream_http_status=200,
                failure_category="sse_business",
                retry_decision="return_error",
                elapsed_ms=10,
            )
        )
        await db.commit()
    rows = (await admin_client.get(f"/api/logs/{log_id}/attempts")).json()
    assert len(rows) == 1 and rows[0]["failure_category"] == "sse_business"
    assert (await admin_client.get(f"/api/logs/{old_id}/attempts")).json() == []
