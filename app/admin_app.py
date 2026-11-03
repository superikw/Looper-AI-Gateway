from __future__ import annotations

import os
import shutil
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psutil
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_service import (
    create_session,
    hash_password,
    new_initial_password,
    require_admin_session,
    revoke_admin_sessions,
    verify_password,
)
from app.config import settings
from app.db import AsyncSessionLocal, get_db
from app.migrations import initialize_database
from app.models import (
    ActiveSession,
    AdminAccount,
    ApiKey,
    AppSetting,
    ModelPrice,
    UsageLog,
    User,
)
from app.schemas import (
    AdminCreate,
    AdminLoginRequest,
    AdminPasswordChange,
    ModelPriceIn,
    SettingUpdate,
    UserProvision,
)
from app.utils import make_api_key, mask_key, sha256_text, utcnow


@asynccontextmanager
async def lifespan(_: FastAPI):
    await initialize_database()
    async with AsyncSessionLocal() as db:
        days = settings.log_retention_days
        await db.execute(
            delete(UsageLog).where(
                UsageLog.created_at < utcnow() - timedelta(days=days)
            )
        )
        await db.commit()
    yield


admin_app = FastAPI(title="Looper AI Gateway Admin", lifespan=lifespan)


@admin_app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return ADMIN_HTML


async def current_admin(
    request: Request, db: AsyncSession = Depends(get_db)
) -> AdminAccount:
    return await require_admin_session(request, db)


async def superadmin(admin: AdminAccount = Depends(current_admin)) -> AdminAccount:
    if not admin.is_superadmin:
        raise HTTPException(403, "super administrator required")
    return admin


@admin_app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@admin_app.post("/api/login")
async def login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    admin = await db.scalar(select(AdminAccount).where(AdminAccount.username == payload.username))
    if not admin or not admin.enabled or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(401, "invalid username or password")
    token = await create_session(db, admin)
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=7 * 86400,
    )
    return {"ok": True, "username": admin.username, "is_superadmin": admin.is_superadmin,
            "must_change_password": admin.must_change_password}


@admin_app.post("/api/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("admin_session")
    if token:
        from app.models import AdminSession

        session = await db.scalar(select(AdminSession).where(AdminSession.token_hash == sha256_text(token)))
        if session:
            session.revoked_at = utcnow()
            await db.commit()
    response.delete_cookie("admin_session")
    return {"ok": True}


@admin_app.get("/api/me")
async def me(admin: AdminAccount = Depends(current_admin)):
    return {"id": admin.id, "username": admin.username, "is_superadmin": admin.is_superadmin,
            "must_change_password": admin.must_change_password}


@admin_app.post("/api/password")
async def change_password(payload: AdminPasswordChange, request: Request, admin: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    if payload.current_password and not verify_password(payload.current_password, admin.password_hash):
        raise HTTPException(400, "current password is incorrect")
    if not payload.current_password and not admin.must_change_password:
        raise HTTPException(400, "current password is required")
    admin.password_hash = hash_password(payload.new_password)
    admin.must_change_password = False
    admin.updated_at = utcnow()
    token = request.cookies.get("admin_session")
    await revoke_admin_sessions(db, admin.id, sha256_text(token) if token else None)
    return {"ok": True, "must_change_password": False}


@admin_app.get("/api/admins")
async def list_admins(_: AdminAccount = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(AdminAccount).order_by(AdminAccount.id))).all()
    return [
        {
            "id": row.id,
            "username": row.username,
            "is_superadmin": row.is_superadmin,
            "must_change_password": row.must_change_password,
            "enabled": row.enabled,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@admin_app.post("/api/admins")
async def create_admin(payload: AdminCreate, _: AdminAccount = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    if await db.scalar(select(AdminAccount).where(AdminAccount.username == payload.username)):
        raise HTTPException(409, "username already exists")
    password = new_initial_password()
    admin = AdminAccount(username=payload.username, password_hash=hash_password(password))
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return {"id": admin.id, "username": admin.username, "initial_password": password}


@admin_app.post("/api/admins/{admin_id}/reset-password")
async def reset_admin_password(admin_id: int, _: AdminAccount = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    admin = await db.get(AdminAccount, admin_id)
    if not admin or admin.is_superadmin:
        raise HTTPException(404, "administrator not found")
    password = new_initial_password()
    admin.password_hash = hash_password(password)
    admin.must_change_password = True
    admin.updated_at = utcnow()
    await revoke_admin_sessions(db, admin.id)
    return {"ok": True, "username": admin.username, "initial_password": password}


@admin_app.get("/api/users")
async def users(_: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.id))).all()
    result = []
    tz_name = await setting(db, "timezone", settings.timezone or _local_timezone())
    for user in rows:
        keys = (await db.scalars(select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.deleted_at.is_(None)))).all()
        usage = {}
        for period in ("day", "yesterday", "week", "month"):
            start = _period_start(period, tz_name)
            end = _period_start("day", tz_name) if period == "yesterday" else utcnow()
            stats = (await db.execute(select(
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.cost_usd), 0),
            ).where(
                UsageLog.user_id == user.id,
                UsageLog.created_at >= start,
                UsageLog.created_at < end,
            ))).one()
            usage[period] = {"requests": stats[0], "tokens": stats[1], "cost_usd": stats[2]}
        result.append({"id": user.id, "name": user.name, "note": user.note, "enabled": user.enabled,
                       "created_at": user.created_at, "usage": usage, "keys": [
                           {"id": k.id, "name": k.name, "preview": k.key_preview, "enabled": k.enabled}
                           for k in keys
                       ]})
    return result


@admin_app.post("/api/users")
async def create_user(payload: UserProvision, _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    if await db.scalar(select(User).where(User.name == payload.name)):
        raise HTTPException(409, "username already exists")
    user = User(name=payload.name, note=payload.note)
    db.add(user)
    await db.flush()
    raw_key = make_api_key()
    db.add(ApiKey(user_id=user.id, name=payload.key_name, key_hash=sha256_text(raw_key), key_preview=mask_key(raw_key)))
    await db.commit()
    return {"id": user.id, "name": user.name, "api_key": raw_key}


@admin_app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(404, "user not found")
    user.enabled = False
    user.deleted_at = utcnow()
    await db.execute(ApiKey.__table__.update().where(ApiKey.user_id == user_id).values(enabled=False, deleted_at=utcnow()))
    await db.commit()
    return {"ok": True}


@admin_app.get("/api/prices")
async def prices(_: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(ModelPrice).order_by(ModelPrice.model))).all()


@admin_app.put("/api/prices")
async def put_price(payload: ModelPriceIn, _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    price = await db.scalar(select(ModelPrice).where(ModelPrice.model == payload.model))
    if not price:
        price = ModelPrice(model=payload.model)
        db.add(price)
    for key, value in payload.model_dump().items():
        setattr(price, key, value)
    price.updated_at = utcnow()
    await db.commit()
    return price


async def setting(db: AsyncSession, key: str, default: str) -> str:
    row = await db.get(AppSetting, key)
    return row.value if row else default


@admin_app.get("/api/settings")
async def get_settings(_: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return {"timezone": await setting(db, "timezone", settings.timezone or _local_timezone()),
            "log_retention_days": int(await setting(db, "log_retention_days", str(settings.log_retention_days)))}


@admin_app.put("/api/settings")
async def update_settings(payload: SettingUpdate, _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    changes = payload.model_dump(exclude_none=True)
    if "timezone" in changes:
        try:
            ZoneInfo(changes["timezone"])
        except ZoneInfoNotFoundError:
            raise HTTPException(400, "invalid timezone") from None
    for key, value in changes.items():
        row = await db.get(AppSetting, key)
        if not row:
            row = AppSetting(key=key, value=str(value))
            db.add(row)
        else:
            row.value = str(value)
        row.updated_at = utcnow()
    await db.commit()
    return await get_settings(_, db)


def _local_timezone() -> str:
    return datetime.now().astimezone().tzinfo.key if getattr(datetime.now().astimezone().tzinfo, "key", None) else "UTC"


def _period_start(name: str, tz_name: str) -> datetime:
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = UTC
    now = datetime.now(zone)
    if name == "day":
        start = now.date()
    elif name == "yesterday":
        start = now.date() - timedelta(days=1)
    elif name == "week":
        start = now.date() - timedelta(days=now.weekday())
    elif name == "month":
        start = now.date().replace(day=1)
    else:
        start = now.date()
    if name == "yesterday":
        start_time = datetime.combine(start, time.min, zone)
    else:
        start_time = datetime.combine(start, time.min, zone)
    return start_time.astimezone(UTC).replace(tzinfo=None)


@admin_app.get("/api/dashboard")
async def dashboard(_: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    tz_name = await setting(db, "timezone", settings.timezone or _local_timezone())
    totals = {}
    for name in ("day", "yesterday", "week", "month"):
        start = _period_start(name, tz_name)
        end = _period_start("day", tz_name) if name == "yesterday" else utcnow()
        row = (await db.execute(select(func.count(UsageLog.id), func.coalesce(func.sum(UsageLog.total_tokens), 0), func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(UsageLog.created_at >= start, UsageLog.created_at < end))).one()
        totals[name] = {"requests": row[0], "tokens": row[1], "cost_usd": row[2]}
    return {"periods": totals, "system": _system_metrics(), "active_sessions": await db.scalar(select(func.count(ActiveSession.id)).where(ActiveSession.finished_at.is_(None)))}


@admin_app.get("/api/system")
async def system(_: AdminAccount = Depends(current_admin)):
    return _system_metrics()


def _system_metrics() -> dict:
    disk = shutil.disk_usage("/")
    return {"cpu_percent": psutil.cpu_percent(interval=None), "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": round(disk.used * 100 / disk.total, 2), "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else []}


@admin_app.get("/api/logs")
async def logs(limit: int = 100, offset: int = 0, _: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    limit = min(max(limit, 1), 500)
    rows = (await db.execute(select(UsageLog, User.name, ApiKey.name).join(User, User.id == UsageLog.user_id).join(ApiKey, ApiKey.id == UsageLog.api_key_id).order_by(UsageLog.id.desc()).limit(limit).offset(max(offset, 0)))).all()
    return [{"id": log.id, "created_at": log.created_at, "user": username, "key": keyname, "model": log.model,
             "status_code": log.status_code, "input_tokens": log.prompt_tokens, "output_tokens": log.completion_tokens,
             "total_tokens": log.total_tokens, "cost_usd": log.cost_usd, "error": log.error_message,
             "upstream_request_id": log.upstream_request_id} for log, username, keyname in rows]


@admin_app.delete("/api/logs")
async def clear_logs(all_logs: bool = False, _: AdminAccount = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    if all_logs:
        result = await db.execute(delete(UsageLog))
    else:
        days = int(await setting(db, "log_retention_days", str(settings.log_retention_days)))
        result = await db.execute(delete(UsageLog).where(UsageLog.created_at < utcnow() - timedelta(days=days)))
    await db.commit()
    return {"deleted": result.rowcount or 0}


@admin_app.get("/api/sessions")
async def sessions(_: AdminAccount = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    stale_before = utcnow() - timedelta(hours=2)
    stale = (await db.scalars(select(ActiveSession).where(
        ActiveSession.finished_at.is_(None), ActiveSession.last_event_at < stale_before
    ))).all()
    for session in stale:
        session.status = "stale"
        session.finished_at = utcnow()
    if stale:
        await db.commit()
    rows = (await db.execute(select(ActiveSession, User.name).join(User, User.id == ActiveSession.user_id).where(ActiveSession.finished_at.is_(None)).order_by(ActiveSession.started_at.desc()))).all()
    return [{"id": row.id, "user": name, "model": row.model, "path": row.path, "status": row.status,
             "started_at": row.started_at, "last_event_at": row.last_event_at} for row, name in rows]


ADMIN_HTML = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Looper 管理后台</title>
<style>body{font:14px system-ui;margin:28px auto;padding:0 18px;max-width:1250px;color:#202124}button{margin:4px;padding:7px 11px;border:1px solid #bbb;border-radius:5px;background:#fff;cursor:pointer}input{padding:8px;margin:4px;border:1px solid #bbb;border-radius:5px}.card{display:inline-block;vertical-align:top;border:1px solid #ddd;border-radius:8px;padding:14px;margin:6px;min-width:170px}.danger{color:#b00020}table{border-collapse:collapse;width:100%;margin:14px 0 28px}td,th{border-bottom:1px solid #ddd;padding:8px;text-align:left}.muted{color:#777}</style>
<div id='app'><h1>Looper 管理后台</h1><form onsubmit="event.preventDefault();fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:this.u.value,password:this.p.value})}).then(r=>r.ok?location.reload():r.json().then(e=>Promise.reject(Error(e.detail||'登录失败')))).catch(e=>alert(e.message))"><input name=u placeholder=用户名 required><input name=p type=password placeholder=密码 required><button>登录</button></form><p class=muted>正在检查登录状态...</p></div><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(u,o={}){let controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);try{let r=await fetch(u,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(o.headers||{})},...o,signal:controller.signal});let d=await r.json();if(!r.ok)throw Error(d.detail||d.error?.message||'请求失败');return d}catch(e){if(e.name==='AbortError')throw Error('管理端 API 请求超时，请检查 looper-ai-admin 日志');throw e}finally{clearTimeout(timer)}}
function login(){document.querySelector('#app').innerHTML='<h1>Looper 管理后台</h1><form onsubmit="event.preventDefault();api(\'/api/login\',{method:\'POST\',body:JSON.stringify({username:this.u.value,password:this.p.value})}).then(load).catch(e=>alert(e.message))"><input name=u placeholder=用户名 required><input name=p type=password placeholder=密码 required><button>登录</button></form>'}
function passwordForm(me){document.querySelector('#app').innerHTML=`<h1>修改密码</h1><p>管理员 ${esc(me.username)} 设置密码。</p><form onsubmit="event.preventDefault();api('/api/password',{method:'POST',body:JSON.stringify({current_password:this.c.value||null,new_password:this.p.value})}).then(load).catch(e=>alert(e.message))"><input name=c type=password placeholder='当前密码（首次登录可留空）'><input name=p type=password minlength=12 placeholder='新密码（至少 12 位）' required><button>保存密码</button></form>`}
async function createUser(){let name=prompt('用户名');if(!name)return;let d=await api('/api/users',{method:'POST',body:JSON.stringify({name,key_name:'default'})});alert('API Key 只显示一次：\\n'+d.api_key);load()}
async function removeUser(id){if(confirm('删除该用户并撤销其 API Key？')){await api('/api/users/'+id,{method:'DELETE'});load()}}
async function createAdmin(){let username=prompt('管理员用户名');if(!username)return;let d=await api('/api/admins',{method:'POST',body:JSON.stringify({username})});alert('初始密码只显示一次：\n'+d.initial_password);load()}
async function resetAdmin(id){if(!confirm('重置该管理员密码并踢下线？'))return;let d=await api('/api/admins/'+id+'/reset-password',{method:'POST'});alert('新的初始密码只显示一次：\n'+d.initial_password);load()}
async function setPrice(){let model=prompt('模型');if(!model)return;let input=Number(prompt('输入价格 USD/百万 token','0'));let output=Number(prompt('输出价格 USD/百万 token','0'));await api('/api/prices',{method:'PUT',body:JSON.stringify({model,input_usd_per_million:input,output_usd_per_million:output,source:'manual'})});load()}
async function saveSettings(){let tz=prompt('IANA 时区，例如 Asia/Shanghai');if(!tz)return;await api('/api/settings',{method:'PUT',body:JSON.stringify({timezone:tz})});load()}
async function clearLogs(){if(confirm('删除保留期之外的日志？')){await api('/api/logs',{method:'DELETE'});load()}}
async function load(){try{let me=await api('/api/me');if(me.must_change_password)return passwordForm(me);let [dash,users,logs,sessions,settings]=await Promise.all([api('/api/dashboard'),api('/api/users'),api('/api/logs?limit=50'),api('/api/sessions'),api('/api/settings')]);let admins=[],prices=[];if(me.is_superadmin)[admins,prices]=await Promise.all([api('/api/admins'),api('/api/prices')]);document.querySelector('#app').innerHTML=`<h1>Looper 管理后台</h1><p>管理员：${esc(me.username)} <button onclick="api('/api/logout',{method:'POST'}).then(()=>location.reload())">退出</button> <button onclick="passwordForm({username:${JSON.stringify(me.username)}})">修改密码</button></p><h2>仪表盘</h2>${Object.entries(dash.periods).map(([k,v])=>`<div class=card><b>${esc(k)}</b><br>${v.requests} 请求<br>${v.tokens} tokens<br>$${Number(v.cost_usd).toFixed(6)}</div>`).join('')}<div class=card>CPU ${dash.system.cpu_percent}%<br>内存 ${dash.system.memory_percent}%<br>磁盘 ${dash.system.disk_percent}%<br>活动会话 ${dash.active_sessions}</div><h2>设置 <button onclick="saveSettings()">设置时区</button> <button onclick="clearLogs()">清理过期日志</button></h2><p class=muted>当前统计时区：${esc(settings.timezone)}；日志保留 ${settings.log_retention_days} 天。</p><h2>模型价格 <button onclick="setPrice()">设置价格</button></h2>${prices.map(p=>`<span class=card>${esc(p.model)}<br>输入 $${p.input_usd_per_million}/M<br>输出 $${p.output_usd_per_million}/M</span>`).join('')}${me.is_superadmin?'<h2>管理员 <button onclick=\"createAdmin()\">创建管理员</button></h2><p>'+admins.map(a=>esc(a.username)+'（'+(a.is_superadmin?'超级管理员':'普通管理员')+'）'+(a.is_superadmin?'':' <button onclick=\"resetAdmin('+a.id+')\">重置密码</button>')).join('、')+'</p>':''}<h2>用户 <button onclick="createUser()">创建用户并生成 API Key</button></h2><table><tr><th>用户</th><th>状态</th><th>API Key</th><th>操作</th></tr>${users.map(u=>`<tr><td>${esc(u.name)}</td><td>${u.enabled?'启用':'停用'}</td><td>${u.keys.map(k=>esc(k.preview)).join(', ')}</td><td><button onclick="removeUser(${u.id})">删除</button></td></tr>`).join('')}</table><h2>活动会话</h2><table><tr><th>用户</th><th>模型</th><th>路径</th><th>状态</th><th>开始</th></tr>${sessions.map(s=>`<tr><td>${esc(s.user)}</td><td>${esc(s.model)}</td><td>${esc(s.path)}</td><td>${esc(s.status)}</td><td>${esc(s.started_at)}</td></tr>`).join('')}</table><h2>最近请求</h2><table><tr><th>时间</th><th>用户</th><th>模型</th><th>状态</th><th>Tokens</th><th>费用</th><th>错误</th></tr>${logs.map(x=>`<tr><td>${esc(x.created_at)}</td><td>${esc(x.user)}</td><td>${esc(x.model)}</td><td>${x.status_code}</td><td>${x.total_tokens}</td><td>$${Number(x.cost_usd).toFixed(6)}</td><td>${esc(x.error)}</td></tr>`).join('')}</table><p class=muted>统计时区：${esc(settings.timezone)}；日志保留 ${settings.log_retention_days} 天。</p>`}catch(e){if(String(e.message).includes('login')||String(e.message).includes('expired'))login();else document.querySelector('#app').innerHTML='<p class=danger>'+esc(e.message)+'</p>'}}load();setInterval(load,15000)
</script>"""
