'use strict';
const app = document.querySelector('#app');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let me, config = {timezone:'UTC'}, page = location.pathname, generation = 0, offset = 0;
const names = {'/dashboard':'仪表盘','/users':'用户管理','/logs':'请求日志','/upstreams':'上游账户','/prices':'模型价格','/settings':'设置'};
function notice(message) { document.querySelector('#notice').textContent = message; }
async function api(url, options = {}) {
  const response = await fetch(url, {...options, credentials:'same-origin', headers:{'Content-Type':'application/json', ...options.headers}, signal:AbortSignal.timeout(15000)});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && url !== '/api/login') { me = null; showLogin(); }
    const error = new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || '请求失败'));
    error.status = response.status; throw error;
  }
  return data;
}
const send = (url, method, body) => api(url, {method, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
function date(value) {
  if (!value) return '—';
  // Older servers also return UTC without an offset.
  const utc = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value + 'Z';
  const parsed = new Date(utc);
  return Number.isNaN(parsed.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', {timeZone:config.timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(parsed);
}
const money = value => '$' + Number(value || 0).toFixed(6);
const table = (headers, rows) => `<div class="table-wrap"><table><thead><tr>${headers.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.join('') || `<tr><td colspan="${headers.length}" class="muted">暂无数据</td></tr>`}</tbody></table></div>`;
const button = (action, label, id='') => `<button type="button" data-action="${action}" data-id="${esc(id)}">${label}</button>`;
function modal(title, html, submit) {
  document.querySelector('dialog')?.remove();
  const dialog = document.createElement('dialog');
  dialog.innerHTML = `<form method="dialog"><h2>${esc(title)}</h2>${html}<div class="toolbar"><button type="submit">保存</button><button type="button" data-close>取消</button></div></form>`;
  app.append(dialog);
  dialog.querySelector('[data-close]').onclick = () => dialog.close();
  dialog.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const save = dialog.querySelector('button[type=submit]'); save.disabled = true;
    try { await submit(new FormData(event.target)); dialog.close(); await render(); notice('已保存'); }
    catch (error) { notice(error.message); } finally {save.disabled = false;}
  };
  dialog.showModal();
}
function secret(title, value) {
  const dialog = document.createElement('dialog');
  dialog.innerHTML = `<h2>${esc(title)}</h2><p>请立即保存，关闭后不再显示。</p><pre></pre><button type="button">关闭</button>`;
  dialog.querySelector('pre').textContent = value;
  dialog.querySelector('button').onclick = () => {dialog.close(); dialog.remove();};
  document.body.append(dialog); dialog.showModal();
}
function showLogin() {
  generation++;
  app.innerHTML = `<main class="login"><h1>Looper 管理后台</h1><form id="login-form" method="post" action="/api/login"><label>用户名<input id="username" autocomplete="username" required maxlength="128"></label><label>密码<input id="password" type="password" autocomplete="current-password" required maxlength="256"></label><button type="submit">登录</button></form></main>`;
}
function passwordForm() {
  const first = me.must_change_password;
  modal('修改密码', `<p>${first?'首次登录需要设置新密码。':''}</p><label>当前密码<input type="password" name="current" autocomplete="current-password" ${first?'':'required'}></label><label>新密码<input type="password" name="next" autocomplete="new-password" minlength="12" maxlength="256" required></label>`, async data => {
    await send('/api/password','POST',{current_password:data.get('current') || null,new_password:data.get('next')});
    me.must_change_password = false;
  });
}
function shell() {
  app.innerHTML = `<div class="shell"><aside><strong>Looper Gateway</strong><nav>${Object.entries(names).map(([path,name])=>`<a href="${path}" ${page===path?'aria-current="page"':''}>${name}</a>`).join('')}</nav></aside><div class="workspace"><header><div><h1>${esc(names[page] || names['/dashboard'])}</h1><span class="muted">显示时区：${esc(config.timezone)}</span></div><div class="toolbar"><span>${esc(me.username)}</span>${button('password','修改密码')}${button('logout','退出')}</div></header><main id="content" aria-live="polite">正在加载…</main></div></div>`;
}
async function render() {
  if (!me) return;
  if (me.must_change_password) {app.innerHTML = '<main class="login"><h1>请先修改初始密码</h1></main>';passwordForm(); return;}
  const current = ++generation;
  config = await api('/api/settings');
  let html = '';
  if (page === '/dashboard') {
    const [dash,sessions,logs] = await Promise.all([api('/api/dashboard'),api('/api/sessions?recent_seconds=300'),api('/api/logs?limit=10')]);
    const periods = {day:'今天',yesterday:'昨天',week:'本周',month:'本月'};
    html = `<div class="cards">${Object.entries(dash.periods).map(([key,v])=>`<section class="card"><h3>${periods[key]}</h3><div class="metric">${v.requests} 用户请求</div><p>${v.tokens} tokens · ${money(v.cost_usd)}</p></section>`).join('')}</div><section class="panel">CPU ${dash.system.cpu_percent}% · 内存 ${dash.system.memory_percent}% · 磁盘 ${dash.system.disk_percent}% · 活动会话 <span id="active-session-count">${sessions.filter(s=>!s.finished_at).length}</span></section><h2>活动会话</h2><p class="muted">每 3 秒刷新；同时保留最近 5 分钟已结束的会话。</p><div id="session-list">${sessionTable(sessions)}</div><h2>最近请求</h2>${logTable(logs)}`;
  } else if (page === '/users') {
    const [users,pool] = await Promise.all([api('/api/users'),api('/api/upstreams')]);
    window.viewUsers = users; window.viewAccounts = pool.accounts;
    html = `<div class="toolbar">${button('create-user','创建用户并生成 API Key')}</div><p class="muted">可分配多个上游账户。未分配的用户无法发起上游请求。</p>${table(['用户','创建时间','状态','API Key','今日用量','上游账户','请求限制','操作'],users.map(u=>`<tr><td>${esc(u.name)}</td><td>${date(u.created_at)}</td><td>${u.enabled?'启用':'停用'}</td><td>${u.keys.map(k=>esc(k.preview)).join('<br>')}</td><td>${u.usage.day.requests} 请求<br>${u.usage.day.tokens} tokens<br>${money(u.usage.day.cost_usd)}</td><td>${u.upstream_ids.map(id=>esc(pool.accounts.find(a=>a.id===id)?.display_name || '#'+id)).join('<br>') || '未分配'}</td><td>并发 ${u.max_concurrency}<br>RPM ${u.max_rpm || '不限'}<br>间隔 ${u.min_interval_seconds}s</td><td>${button('user-limits','请求限制',u.id)}${button('user-routes','分配上游',u.id)}${button('delete-user','删除',u.id)}</td></tr>`))}`;
  } else if (page === '/logs') {
    const logs = await api(`/api/logs?limit=50&offset=${offset}`);
    html = `<div class="toolbar">${button('refresh','刷新')}${me.is_superadmin?button('clear-logs','清理过期日志'):''}</div><p class="muted">每次上游尝试单独记录；同一用户请求共用短 ID，/0 为首次，/1 为第一次重试。未调用上游的本地拒绝同样保留记录。</p>${logTable(logs)}<div class="pager">${offset?button('previous','上一页'):''}<span>第 ${offset/50+1} 页</span>${logs.length===50?button('next','下一页'):''}</div>`;
  } else if (page === '/prices') {
    const [prices,unmatched] = await Promise.all([api('/api/prices'),api('/api/prices/unmatched')]); window.viewPrices = prices;
    html = `<div class="toolbar">${button('price','添加价格')}</div><p class="muted">USD / 百万 tokens。新请求遇到未定价模型时自动精确匹配，优先 LiteLLM 的原厂条目，再使用 OpenRouter；手动价格优先且不会被自动覆盖。未匹配模型费用暂记为 0，补价只影响后续请求。</p>${unmatched.length?`<p class="danger">待设置价格：${unmatched.map(esc).join('、')}</p>`:''}<p><a href="https://developers.openai.com/api/docs/pricing" target="_blank" rel="noreferrer">OpenAI 官方报价</a> · <a href="https://platform.claude.com/docs/en/about-claude/pricing" target="_blank" rel="noreferrer">Claude 官方报价</a></p>${table(['模型','输入','输出','缓存读取','来源','更新时间','操作'],prices.map(p=>`<tr><td>${esc(p.model)}</td><td>${p.input_usd_per_million}</td><td>${p.output_usd_per_million}</td><td>${p.cached_input_usd_per_million}</td><td>${esc(p.source)}</td><td>${date(p.updated_at)}</td><td>${button('price','编辑',p.id)}</td></tr>`))}`;
  } else if (page === '/settings') {
    const admins = me.is_superadmin ? await api('/api/admins') : [];
    html = `<section class="panel"><h2>系统设置</h2><form id="settings-form" class="compact"><label>IANA 时区<input name="timezone" value="${esc(config.timezone)}" placeholder="Asia/Shanghai" required></label><label>日志保留天数<input name="days" type="number" min="1" max="3650" value="${config.log_retention_days}" required></label><button>保存设置</button></form></section>${me.is_superadmin?`<section class="panel"><h2>管理员</h2>${button('create-admin','创建管理员')}${table(['用户名','角色','创建时间','操作'],admins.map(a=>`<tr><td>${esc(a.username)}</td><td>${a.is_superadmin?'超级管理员':'普通管理员'}</td><td>${date(a.created_at)}</td><td>${a.is_superadmin?'':button('reset-admin','重置密码',a.id)}</td></tr>`))}</section>`:''}`;
  } else if (page === '/upstreams') {
    const [pool,users] = await Promise.all([api('/api/upstreams'),api('/api/users')]);
    window.viewAccounts = pool.accounts; window.viewUsers = users;
    html = `<div class="toolbar">${button('oauth','添加上游账户（OAuth）')}${button('refresh','刷新列表')}</div><p class="muted">认证模式：${esc(pool.auth_mode)}。新账户默认无用户路由；分配后参与调度。会话数为当前账户的进行中请求数，用量额度需要手动刷新。</p><div class="grid">${pool.accounts.map(a=>`<section class="card"><h2>${esc(a.display_name || a.account_id || '账户 #'+a.id)} <span class="badge">${esc(a.status)}</span></h2><p>账户 ID：${esc(a.account_id || '未知')}<br>凭证过期：${date(a.expires_at)}<br>冷却重置：${date(a.cooldown_until)}<br>最近使用：${date(a.last_used_at)}</p><p>${a.request_count} 请求 · ${a.failure_count} 失败<br>${a.total_tokens} tokens · ${a.active_sessions} 活动会话<br>RPM ${a.max_rpm} · 并发 ${a.max_concurrency} · 最小间隔 ${a.min_interval_seconds}s</p>${quotaHTML(a.quota)}<p>路由用户：${a.user_ids.map(id=>esc(users.find(u=>u.id===id)?.name || '#'+id)).join('、') || '未分配'}</p><div class="toolbar">${button('account-routes','分配用户',a.id)}${button('account-edit','维护',a.id)}${button('quota','刷新额度',a.id)}${button('account-toggle',a.enabled?'停用':'启用',a.id)}${button('account-delete','删除',a.id)}</div></section>`).join('') || '<p>暂无上游账户，请添加。</p>'}</div>`;
  }
  if (current !== generation || !me) return;
  shell(); document.querySelector('#content').innerHTML = html;
}
function logTable(logs) {
  return table(['时间','用户 / Key','接口','上游账户','请求 ID / 重试','模型','状态','输入 / 输出','Tokens','费用','错误','尝试链路'],logs.map(x=>`<tr><td>${date(x.created_at)}</td><td>${esc(x.user)}<br><small>${esc(x.key)}</small></td><td>${interfaceHTML(x.path)}</td><td>${esc(x.upstream_display_name || '—')}${x.upstream_credential_id ? `<small>本地 #${x.upstream_credential_id}</small>` : ''}</td><td>${esc(x.display_request_id || x.gateway_request_id || '—')}<small>上游 ID：${esc(x.upstream_request_id || '—')}</small></td><td>${esc(x.model)}</td><td>${x.status_code}${x.upstream_http_status != null ? `<small>上游 HTTP ${x.upstream_http_status}</small>` : ''}${x.failure_category ? `<small>${esc(x.failure_category)}</small>` : ''}${x.elapsed_ms != null ? `<small>${x.elapsed_ms} ms</small>` : ''}</td><td>${x.input_tokens} / ${x.output_tokens}</td><td>${x.total_tokens}</td><td>${money(x.cost_usd)}</td><td class="error">${esc(x.error)}</td><td>${x.gateway_request_id?button('attempts','查看尝试',x.id):'—'}</td></tr>`));
}
function interfaceHTML(path) {
  const label = {'/v1/responses':'Responses','/v1/chat/completions':'Chat Completions'}[path];
  return label ? `${label}<small>${esc(path)}</small>` : esc(path || '—');
}
function sessionTable(sessions) {
  const statuses = {running:'进行中',completed:'已完成',failed:'失败',stale:'已超时'};
  return table(['用户','模型','接口','状态','开始时间','最近活动','结束时间'],sessions.map(s=>`<tr><td>${esc(s.user)}</td><td>${esc(s.model)}</td><td>${interfaceHTML(s.path)}</td><td>${esc(statuses[s.status] || s.status)}</td><td>${date(s.started_at)}</td><td>${date(s.last_event_at)}</td><td>${date(s.finished_at)}</td></tr>`));
}
let refreshingSessions = false;
async function refreshSessions() {
  if (!me || page !== '/dashboard' || refreshingSessions || document.hidden) return;
  const current = generation;
  refreshingSessions = true;
  try {
    const sessions = await api('/api/sessions?recent_seconds=300');
    if (current !== generation || page !== '/dashboard') return;
    const list = document.querySelector('#session-list');
    const count = document.querySelector('#active-session-count');
    if (list) list.innerHTML = sessionTable(sessions);
    if (count) count.textContent = sessions.filter(s=>!s.finished_at).length;
  } finally {refreshingSessions = false;}
}
function quotaHTML(quota) {
  if (!quota) return '<p class="muted">尚未获取上游额度</p>';
  return `<p>套餐：${esc(quota.plan_type || '未知')}<br>${[['primary','主窗口'],['secondary','次窗口']].map(([key,label])=>quota[key]?`${label}已用 ${quota[key].used_percent}%<br>重置：${date(quota[key].reset_at)}`:`${label}：未提供`).join('<br>')}<small>额度更新时间：${date(quota.checked_at)}</small></p>`;
}
function routeForm(kind, id) {
  const userSide = kind === 'user-routes';
  const subject = (userSide?window.viewUsers:window.viewAccounts).find(x=>x.id===id);
  const choices = userSide?window.viewAccounts:window.viewUsers;
  const selected = userSide?subject.upstream_ids:subject.user_ids;
  modal(userSide?'分配上游账户':'分配路由用户', `<p>支持多选，全部取消即无路由。</p><div class="toolbar">${button('routes-select-all','全选')}${button('routes-invert','反选')}</div>${choices.map(x=>`<label class="check"><input type="checkbox" name="ids" value="${x.id}" ${selected.includes(x.id)?'checked':''}>${esc(x.display_name || x.name || '#'+x.id)}</label>`).join('') || '<p>暂无可分配项。</p>'}`, data => send(userSide?`/api/users/${id}/upstreams`:`/api/upstreams/${id}/users`,'PUT',{[userSide?'upstream_ids':'user_ids']:data.getAll('ids').map(Number)}));
}
async function act(action, id) {
  switch(action) {
    case 'routes-select-all': case 'routes-invert':
      document.querySelectorAll('dialog[open] input[name="ids"]').forEach(input=>{input.checked=action==='routes-select-all' || !input.checked;});
      return;
    case 'attempts': {
      const rows=await api(`/api/logs/${id}/attempts`);
      const dialog=document.createElement('dialog');
      dialog.innerHTML=`<h2>请求尝试链路</h2><p>${esc(rows[0]?.gateway_request_id || '无尝试记录')}</p>${table(['重试次数','上游编号','HTTP','结果分类','错误类型 / 代码','传输异常类型','处理决定','耗时 ms','上游请求 ID'],rows.map(r=>`<tr><td>${r.retry_count ?? r.attempt-1}</td><td>${esc(r.upstream_credential_id ?? '—')}</td><td>${esc(r.upstream_http_status ?? '—')}</td><td>${esc(r.failure_category || '成功')}</td><td>${esc(r.upstream_error_type)} / ${esc(r.upstream_error_code)}</td><td>${esc(r.transport_exception_type)}</td><td>${esc(r.retry_decision)}</td><td>${r.elapsed_ms}</td><td>${esc(r.upstream_request_id)}</td></tr>`))}<button type="button" data-close>关闭</button>`;
      dialog.querySelector('[data-close]').onclick=()=>{dialog.close();dialog.remove();};
      document.body.append(dialog);dialog.showModal();return;
    }
    case 'password': passwordForm(); return;
    case 'logout': await send('/api/logout','POST'); me=null; showLogin(); return;
    case 'refresh': await render(); return;
    case 'next': offset+=50; await render(); return;
    case 'previous': offset=Math.max(0,offset-50); await render(); return;
    case 'create-user': modal('创建用户', '<label>用户名<input name="name" required maxlength="128"></label><label>备注<input name="note" maxlength="255"></label><p>创建后请为该用户分配上游账户。</p>', async data=>{
      const result=await send('/api/users','POST',{name:data.get('name'),note:data.get('note')});
      // Render first so the one-time secret dialog survives page refresh.
      await render(); secret('API Key（只显示一次）',result.api_key);
    }); return;
    case 'delete-user': if(confirm('删除用户并撤销所有 API Key？')) await send('/api/users/'+id,'DELETE'); break;
    case 'user-routes': case 'account-routes': routeForm(action,id); return;
    case 'user-limits': {
      const u=window.viewUsers.find(u=>u.id===id);
      modal('下游用户请求限制',`<p>同一用户的所有 API Key 共用限制。超限返回 429，不排队。</p><label>最大并发<input name="concurrency" type="number" min="1" max="10000" value="${u.max_concurrency}" required></label><label>每分钟请求数（0 表示不限）<input name="rpm" type="number" min="0" max="100000" value="${u.max_rpm}" required></label><label>最小请求间隔（秒）<input name="interval" type="number" min="0" max="3600" step="any" value="${u.min_interval_seconds}" required></label>`,data=>send(`/api/users/${id}/limits`,'PATCH',{max_concurrency:Number(data.get('concurrency')),max_rpm:Number(data.get('rpm')),min_interval_seconds:Number(data.get('interval'))})); return;
    }
    case 'price': {
      const p=window.viewPrices.find(p=>p.id===id) || {};
      modal('模型价格', `<label>模型 ID<input name="model" value="${esc(p.model)}" maxlength="128" required></label>${[['input','输入',p.input_usd_per_million],['output','输出',p.output_usd_per_million],['cached','缓存读取',p.cached_input_usd_per_million]].map(([name,label,value])=>`<label>${label}（USD / 百万 tokens）<input name="${name}" type="number" min="0" step="any" value="${value??0}" required></label>`).join('')}`,data=>send('/api/prices','PUT',{model:data.get('model'),input_usd_per_million:Number(data.get('input')),output_usd_per_million:Number(data.get('output')),cached_input_usd_per_million:Number(data.get('cached')),source:'manual'})); return;
    }
    case 'create-admin': modal('创建管理员','<label>用户名<input name="name" required maxlength="128"></label>',async data=>{
      const result=await send('/api/admins','POST',{username:data.get('name')}); await render(); secret('初始密码',result.initial_password);
    }); return;
    case 'reset-admin': if(confirm('重置密码并撤销该管理员的登录会话？')) {const result=await send(`/api/admins/${id}/reset-password`,'POST'); secret('新的初始密码',result.initial_password);} return;
    case 'clear-logs': if(confirm('删除保留期限以外的请求日志？')) await send('/api/logs','DELETE'); break;
    case 'oauth': {
      const result=await send('/api/upstreams/oauth/start','POST');
      modal('添加上游账户',`<p>打开授权页完成登录，将浏览器最终跳转的完整回调 URL 粘贴到下方。</p><a href="${esc(result.auth_url)}" target="_blank" rel="noreferrer">打开 OAuth 授权页</a><label>账户名称<input name="name" maxlength="128"></label><label>回调 URL<textarea name="callback" required rows="4"></textarea></label>`, data=>send('/api/upstreams/oauth/complete','POST',{session_id:result.session_id,callback_url:data.get('callback'),name:data.get('name') || null})); return;
    }
    case 'account-edit': {
      const a=window.viewAccounts.find(a=>a.id===id);
      modal('上游账户维护',`<label>名称<input name="name" value="${esc(a.name || '')}" placeholder="留空使用邮箱、名称或账户 ID" maxlength="128"></label><label>每分钟请求数<input name="rpm" type="number" min="1" max="100000" value="${a.max_rpm}" required></label><label>最大并发<input name="concurrency" type="number" min="1" max="10000" value="${a.max_concurrency}" required></label><label>最小请求间隔（秒）<input name="interval" type="number" min="0" max="3600" step="any" value="${a.min_interval_seconds}" required></label><label class="check"><input name="reset" type="checkbox">清除失败冷却</label>`,data=>send('/api/upstreams/'+id,'PATCH',{name:data.get('name'),max_rpm:Number(data.get('rpm')),max_concurrency:Number(data.get('concurrency')),min_interval_seconds:Number(data.get('interval')),reset_cooldown:data.has('reset')})); return;
    }
    case 'account-toggle': await send('/api/upstreams/'+id,'PATCH',{enabled:!window.viewAccounts.find(a=>a.id===id).enabled}); break;
    case 'account-delete': if(confirm('删除上游账户并移除路由？已开始的请求会继续完成。')) await send('/api/upstreams/'+id,'DELETE'); break;
    case 'quota': await send(`/api/upstreams/${id}/usage/refresh`,'POST'); break;
  }
  await render();
}
document.addEventListener('submit',async event=>{
  if(event.target.id==='login-form') {
    event.preventDefault();
    if(document.body.dataset.requireHttps === 'true' && location.protocol !== 'https:' && !['localhost','127.0.0.1','[::1]'].includes(location.hostname)) {notice('请通过 HTTPS 地址登录管理后台');return;}
    const password=document.querySelector('#password');
    const credentials={username:document.querySelector('#username').value,password:password.value};
    password.value='';
    try {await send('/api/login','POST',credentials); credentials.password=''; me=await api('/api/me'); notice(''); await render();} catch(error){notice(error.message);} finally {credentials.password='';}
  } else if(event.target.id==='settings-form') {
    event.preventDefault();const data=new FormData(event.target);
    try {await send('/api/settings','PUT',{timezone:data.get('timezone'),log_retention_days:Number(data.get('days'))});await render();notice('设置已保存，所有时间已按所选时区显示');}catch(error){notice(error.message);}
  }
});
document.addEventListener('click',async event=>{
  const link=event.target.closest('nav a');
  if(link && !event.ctrlKey && !event.metaKey) {event.preventDefault();page=link.getAttribute('href');history.pushState({},'',page);try{await render();}catch(error){notice(error.message);}return;}
  const target=event.target.closest('[data-action]');if(!target)return;
  target.disabled=true;
  try {await act(target.dataset.action,Number(target.dataset.id));}catch(error){notice(error.message);}finally{target.disabled=false;}
});
window.addEventListener('popstate',()=>{page=location.pathname;render().catch(error=>notice(error.message));});
async function boot(){if(!names[page]){page='/dashboard';history.replaceState({},'',page);}try{me=await api('/api/me');await render();}catch(error){if(error.status!==401)notice(error.message);}}
boot();
setInterval(()=>{if(me && !document.querySelector('dialog[open]') && ['/dashboard','/upstreams'].includes(page)) render().catch(error=>notice(error.message));},30000);

setInterval(()=>refreshSessions().catch(error=>notice(error.message)),3000);
