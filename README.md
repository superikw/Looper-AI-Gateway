# Codex Gateway Python

一个给 ChatGPT / Codex 登录态做二次分发的 Python Gateway。

它做的事情很简单：

- 你自己登录一次 Codex / ChatGPT
- Gateway 保存这份上游登录态
- 你创建多个下游用户和 API Key
- 下游统一用 OpenAI 风格接口请求 Gateway
- Gateway 再把请求转发到 ChatGPT Codex backend

当前这版已经验证可用的主路径是：

- 上游认证：OpenClaw 同款 OAuth
- 上游请求层：`https://chatgpt.com/backend-api/codex/responses`
- 下游入口：`/v1/chat/completions`

---

## 1. 适用场景

适合：

- 你自己有 ChatGPT / Codex 账号
- 你想给多个内部用户或脚本分发自己的 Gateway Key
- 你希望把上游登录态和下游调用隔离开

不适合：

- 公网开放转售
- 强 SLA 商用 API 平台
- 需要完全兼容 OpenAI 官方全量 API 的场景

---

## 2. 当前状态

这版已经支持：

- Codex OAuth 登录
- 用户管理
- API Key 创建 / 删除 / 启停
- token usage 统计
- `/v1/chat/completions`
- `/v1/responses`
- `/v1/models`
- Docker / Docker Compose 部署
- `manage.py` 本地管理命令
- 默认自动补 `instructions`

注意：

- `embeddings` 目前不支持这条 Codex backend 路线
- 最稳的模型是 Codex / GPT-5 系列，不是 `gpt-4o-mini`

---

## 3. 项目结构

```text
codex-gateway-python/
├── app/
├── .env.example
├── .env.docker.example
├── Dockerfile
├── docker-compose.yml
├── manage.py
├── README.md
└── requirements.txt
```

---

## 4. 最重要的配置

推荐 `.env` 至少包含这些：

```env
ADMIN_TOKEN=自己生成的随机字符串

UPSTREAM_BASE_URL=https://chatgpt.com/backend-api
UPSTREAM_AUTH_MODE=oauth_manual
UPSTREAM_OPENAI_BETA=responses=experimental
DEFAULT_INSTRUCTIONS=You are a helpful assistant.
DEFAULT_ALLOWED_MODELS=gpt-5.4,gpt-5.3,gpt-5.1-codex-mini,gpt-5.3-codex

OAUTH_AUTHORIZE_URL=https://auth.openai.com/oauth/authorize
OAUTH_TOKEN_URL=https://auth.openai.com/oauth/token
OAUTH_CLIENT_ID=app_EMoamEEZ73f0CkXaXp7hrann
OAUTH_REDIRECT_URI=http://localhost:1455/auth/callback
OAUTH_SCOPE=openid profile email offline_access
OAUTH_ORIGINATOR=pi
OAUTH_CODEX_CLI_SIMPLIFIED_FLOW=true
OAUTH_ID_TOKEN_ADD_ORGANIZATIONS=true
```

### `ADMIN_TOKEN` 是什么？

它就是这个 Gateway 的管理员口令。

不是平台发给你的，也不是 OpenAI 给的，是你自己写进 `.env` 的随机值。

生成一个新的：

```bash
python manage.py generate-admin-token
```

---

## 5. 启动

### Docker Compose

```bash
cd /home/ubuntu/codex-gateway-python
cp .env.docker.example .env
# 按需修改 .env

docker compose up -d --build
```

### 查看状态

```bash
docker compose exec codex-gateway python manage.py doctor
```

你应该能看到：

- `upstream_base_url` 是 `https://chatgpt.com/backend-api`
- `upstream_auth_mode` 是 `oauth_manual`

---

## 6. 登录 Codex / ChatGPT

### 第一步：发起登录

```bash
docker compose exec codex-gateway python manage.py auth-start
```

会返回：

- `session_id`
- `auth_url`
- `state`

### 第二步：浏览器打开 `auth_url`

你自己完成登录。

### 第三步：复制最终回调 URL

浏览器最终会跳到类似：

```text
http://localhost:1455/auth/callback?code=...&state=...
```

### 第四步：完成登录

```bash
docker compose exec codex-gateway python manage.py auth-complete \
  --session-id 1 \
  --callback-url "http://localhost:1455/auth/callback?code=...&state=..."
```

### 第五步：检查登录状态

```bash
docker compose exec codex-gateway python manage.py auth-status
```

如果成功，会看到当前 `credential` 和 `account_id`。

---

## 7. 创建用户和 API Key

### 创建用户

```bash
docker compose exec codex-gateway python manage.py create-user alice --note "team-a"
```

### 查看用户

```bash
docker compose exec codex-gateway python manage.py list-users
```

### 创建 API Key

```bash
docker compose exec codex-gateway python manage.py create-key --user-id 1 --name alice-dev
```

输出里的 `api_key` 明文只会显示一次，记得自己保存。

### 查看 API Key

```bash
docker compose exec codex-gateway python manage.py list-keys
```

---

## 8. 如何调用模型

### 推荐测试命令

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.4","messages":[{"role":"user","content":"hello"}]}'
```

### 说明

- 现在 Gateway 会在你**没有提供 system/developer 消息时**，自动补：

```text
You are a helpful assistant.
```

所以正常情况下，你不需要再每次手动写：

```json
{"role":"system","content":"You are a helpful assistant."}
```

### 当前推荐模型

优先试这些：

- `gpt-5.4`
- `gpt-5.3`
- `gpt-5.1-codex-mini`
- `gpt-5.3-codex`

不建议先测：

- `gpt-4o-mini`

因为 ChatGPT Codex backend 已经明确返回过：

> The 'gpt-4o-mini' model is not supported when using Codex with a ChatGPT account.

---

## 9. 管理命令

最常用的是这些：

```bash
docker compose exec codex-gateway python manage.py doctor
docker compose exec codex-gateway python manage.py auth-start
docker compose exec codex-gateway python manage.py auth-complete --session-id 1 --callback-url "..."
docker compose exec codex-gateway python manage.py auth-status
docker compose exec codex-gateway python manage.py create-user alice --note "team-a"
docker compose exec codex-gateway python manage.py list-users
docker compose exec codex-gateway python manage.py create-key --user-id 1 --name alice-dev
docker compose exec codex-gateway python manage.py list-keys
docker compose exec codex-gateway python manage.py usage
```

---

## 10. 查看用量统计

```bash
docker compose exec codex-gateway python manage.py usage
```

可以看到每个 key 的：

- request_count
- prompt_tokens
- completion_tokens
- total_tokens

---

## 11. 看日志

```bash
docker compose logs --tail=200 codex-gateway
```

当前日志里会包含：

- `upstream_request`
- `upstream_response`

用于排查上游 Codex backend 返回了什么。

---

## 12. 常见问题

### Q1：`model not allowed`
说明你请求的模型不在 `.env` 的：

```env
DEFAULT_ALLOWED_MODELS=...
```

里。

改 `.env` 后重启容器即可。

### Q2：`Instructions are required`
旧版本里，如果没有 system/developer 消息会触发这个问题。

现在这版已经会自动补默认 instructions。重建容器后通常不需要你手工加 system message 了。

### Q3：返回空内容
这通常是上游返回结构和当前提取逻辑还存在边角兼容差异。

先看：

```bash
docker compose logs --tail=200 codex-gateway
```

里的 `upstream_response`。

### Q4：`gpt-4o-mini` 报不支持
这是上游 Codex backend 的限制，不是你本地 Gateway 的 bug。

### Q5：我平时还需要关心 `ADMIN_TOKEN` 吗？
如果你主要通过 `manage.py` 管理，其实平时基本不用碰它。

但 HTTP 管理接口还是依赖它做管理员鉴权，所以服务配置里仍然应该保留。

---

## 13. 当前已知限制

- `embeddings` 还没做这条 Codex backend 路线的适配
- 响应内容提取还在继续打磨，个别请求可能成功但文本为空
- 目前更偏内部自用版，不是强商用版
- 管理后台 UI 还没做

---

## 14. 建议的下一步

如果你要继续把它打磨成更顺手的工具，下一步最值得做的是：

1. 管理后台 Web UI
2. 更完整的 Codex response 内容提取
3. SSE 流式下游输出
4. 上游凭证加密存储
5. per-key 配额 / 限流
