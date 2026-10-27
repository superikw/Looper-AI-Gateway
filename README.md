# Looper-AI-Gateway

GitHub 仓库：https://github.com/superikw/Looper-AI-Gateway

一个用 Python / FastAPI 实现的内部 AI 网关：管理员维护多个 ChatGPT / Codex 上游账号，给下游用户分配独立 API Key，通过 OpenAI 风格接口转发请求并统计用量。

下游 Key 隔离访问权限和用量，默认使用上游账号池，也可固定绑定一个账号。适合内部工具和少量可信用户；当前没有按 Key 配额、跨主机调度或管理 UI。

## 功能与兼容范围

| 入口 | 支持范围 |
| --- | --- |
| `POST /v1/chat/completions` | 文本、用户图片、函数工具及工具结果、普通 JSON 和 SSE 流式输出 |
| `POST /v1/responses` | Responses 输入、工具定义、普通 JSON 和具名 SSE 事件 |
| `GET /v1/models` | 返回本地模型白名单；不代表上游实时可用模型 |
| `POST /v1/embeddings` | 明确返回 400，当前上游路线不支持 |
| `/admin/*` | 用户、Key、上游账号池、OAuth 登录和用量管理 |
| `GET /healthz` | 进程存活检查 |
| `GET /readyz` | 数据库和上游凭证就绪检查；必要时刷新凭证，不发起模型推理 |

Chat Completions 支持 `tools` / `tool_choice`、`parallel_tool_calls`、`temperature`、`reasoning` / `reasoning_effort`、`metadata`、`response_format`（text / json_object / json_schema）。`max_tokens`、`max_completion_tokens` 和 `max_output_tokens` 均映射为上游输出 token 上限，同一请求只能指定一个。

`stream=true` 会增量输出文本、拒绝内容和函数参数。Chat Completions 以 `[DONE]` 结束，可通过 `stream_options.include_usage=true` 获取最终用量；Responses 保留具名事件格式。流式开始前发现错误会返回错误 HTTP 状态；开始后的错误通过 SSE 传递，并在用量日志中记录失败状态。

不支持的参数和内容类型会返回 400，不会静默丢弃。当前不支持多候选结果（`n > 1`）、音频和 `store=true`。缺少 system/developer 指令时自动补充 `DEFAULT_INSTRUCTIONS`。具体模型、参数和工具是否可用仍取决于上游账号与 Codex backend。

协议转换参考：[OpenAI Responses 迁移说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Responses 流式事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)。这些文档描述公开 API 格式，不保证 ChatGPT backend 的全部行为。

## Docker Compose 启动

```bash
git clone https://github.com/superikw/Looper-AI-Gateway.git
cd Looper-AI-Gateway
cp .env.docker.example .env
```

先生成管理员口令：

```bash
docker compose run --rm --build looper-ai-gateway python manage.py generate-admin-token
```

把输出写入 `.env` 的 `ADMIN_TOKEN`，然后启动：

```bash
docker compose up -d --build
docker compose exec looper-ai-gateway python manage.py doctor
curl http://127.0.0.1:34567/healthz
```

管理员口令必须自行配置为至少 32 个字符的随机值。未配置或长度不足时，服务会拒绝启动；本地 CLI 生成口令不受此限制。

默认映射为 `127.0.0.1:34567:8080`：宿主机访问 **34567**，容器内服务监听 **8080**。如需其他机器直接访问，按部署需要修改绑定地址；也可以在宿主机配置反向代理。

Compose 健康检查使用 `/healthz`，因此首次登录之前容器也可以正常启动。`/readyz` 在未登录或凭证不可用时返回 503；返回 200 也不代表已验证模型推理或上游剩余额度。

`env_token` 模式也需要 token 中的 ChatGPT account ID，因为转发必须使用该值设置 `chatgpt-account-id` 请求头。仅配置一个无法提取 account ID 的 token 不满足就绪条件；`/readyz` 不会为此发起上游推理请求。

## 本地运行

需要 Python 3.12 或更新版本。进程间锁使用 POSIX 文件锁，支持 Linux/macOS；Windows 请使用 Docker。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py generate-admin-token
# 将生成的口令写入 .env
python -m app.main
```

本地默认地址为 `http://127.0.0.1:8080`。

## 重要配置

| 配置 | 作用 |
| --- | --- |
| `ADMIN_TOKEN` | 必须手动配置的管理员随机口令，至少 32 个字符 |
| `DATABASE_URL` | 默认 `sqlite+aiosqlite:///./data/gateway.db` |
| `CREDENTIAL_KEY_FILE` | 默认 `./data/credential.key`，首次使用自动生成，文件权限为 0600 |
| `CREDENTIAL_ENCRYPTION_KEY` | 可选 Fernet 密钥；设置后优先于密钥文件，升级时必须保持一致 |
| `UPSTREAM_BASE_URL` | 默认 `https://chatgpt.com/backend-api` |
| `UPSTREAM_AUTH_MODE` | `oauth_manual`、`codex_auth_file` 或 `env_token` |
| `UPSTREAM_TIMEOUT_SECONDS` | 上游 HTTP 超时，默认 120 秒 |
| `UPSTREAM_MAX_ATTEMPTS` | 账号池每个请求最多尝试的不同账号数，默认 3，范围 1–10；固定绑定只尝试一个 |
| `UPSTREAM_FAILURE_COOLDOWN_SECONDS` | 连接或服务端错误后的冷却秒数，默认 10 |
| `UPSTREAM_RATE_LIMIT_COOLDOWN_SECONDS` | 429 缺少有效 Retry-After 时的冷却秒数，默认 60 |
| `MAX_REQUEST_BYTES` | 请求体大小限制，默认 16777216 字节（16 MiB） |
| `DEFAULT_ALLOWED_MODELS` | 全局模型白名单，留空表示不限制；模型名会规范化 |
| `DEFAULT_INSTRUCTIONS` | 缺少指令时使用的默认提示 |
| `OAUTH_*` | OAuth 地址、客户端标识、回调地址和 PKCE 会话配置 |

模型白名单是访问策略，不能用来发现上游模型。项目没有自动选择“最新模型”的逻辑。

多账号池仅在 `oauth_manual` 模式启用，推荐使用此模式。文件和环境变量模式仍使用单个账号。`codex_auth_file` 从 `CODEX_HOME/auth.json` 读取 access token，文件模式不会自行刷新登录态，需要外部维护；`env_token` 需要设置 `UPSTREAM_BEARER_TOKEN`。所有模式都需要可解析的 ChatGPT account ID，不能用任意官方 API Key 代替。

Docker 镜像未安装 Codex CLI；如使用文件模式，需要挂载认证文件。如使用 CLI 登录管理接口，还需自行准备含 Codex CLI 的运行环境。

## 登录上游

```bash
docker compose exec looper-ai-gateway python manage.py auth-start
```

在浏览器中打开输出的 `auth_url`，完成登录，复制最终回调 URL，然后执行：

```bash
docker compose exec looper-ai-gateway python manage.py auth-complete \
  --session-id 1 \
  --name 'work-account' \
  --callback-url 'http://localhost:1455/auth/callback?code=...&state=...'
docker compose exec looper-ai-gateway python manage.py auth-status
curl http://127.0.0.1:34567/readyz
```

这是手动回调流程，不需要 Gateway 监听 1455 端口。授权会话会校验 state 和过期时间，完成后清除 verifier，并仅保存回调已接收的标记。

为每个账号重复以上登录流程，可用不同浏览器配置或退出浏览器登录后切换账号。不同 ChatGPT account ID 分别保存；同一未删除账号重新登录会更新其凭证并重新启用，不覆盖其他账号。`--name` 是可选备注。

OAuth 凭证在到期前 90 秒触发刷新。同一账号的刷新使用进程间锁，避免重复使用轮换中的 refresh token；不同账号可以并行刷新。同一台主机的进程需共享数据库和相同密钥目录。锁和 SQLite 不用于跨主机部署。

退出数据库中所有 OAuth 账号（指定 `--credential-id` 可只退出一个）：

```bash
docker compose exec looper-ai-gateway python manage.py logout
```

CLI 和 HTTP logout 都采用软删除，保留加密凭证与删除时间；默认状态查询、请求转发和刷新都会排除已删除凭证。已删除账号重新登录会创建新记录，绑定旧记录的 Key 需要重新绑定。logout 与登录保存协调锁，并等待相关账号的刷新锁，避免并发刷新恢复已退出的凭证。此操作针对数据库 OAuth 凭证；文件和环境变量模式的认证需在各自来源管理。

## 多账号池与固定绑定

以下命令可在容器内执行；本地运行时直接使用 `python manage.py`。

```bash
python manage.py list-upstreams
python manage.py update-upstream 1 --name 'team-a' --disable
python manage.py update-upstream 1 --enable --reset-cooldown
python manage.py logout --credential-id 1
python manage.py list-upstreams --include-deleted
```

未绑定的 Key 按最近使用时间轮换启用、未删除且不在冷却中的账号。选择记录保存在数据库中，同一主机多个 worker 共享调度顺序，重启后也会保留。暂停、删除或冷却中的账号不会接收新的调度；已经发出的请求会继续处理。

收到明确的 HTTP 401、403、429、500、502、503、504，或建立连接失败时，未绑定请求会在尝试次数内改用其他账号，每个账号最多尝试一次。401/403 冷却 300 秒；429 优先遵循 Retry-After（秒数或 HTTP 日期，限制为 1 秒至 1 天），其他错误使用配置的冷却时间。并发错误不会缩短已有冷却；管理员可手动清除。`/readyz` 至少需要一个可用账号，全部冷却时返回 503。

收到 HTTP 成功响应后，正文错误或流中断不会切号；读写超时也不会自动重放。流式输出始终来自同一个账号。服务端 5xx 重试无法保证上游完全没有处理过前一次请求。账号池不读取上游剩余额度，也不保证不同账号支持完全相同的模型。

固定绑定示例：

```bash
python manage.py create-key --user-id 1 --name dedicated --upstream-id 2
python manage.py bind-key 1 --upstream-id 2
python manage.py bind-key 1 --auto
```

固定绑定的账号不可用时，该 Key 返回 503，不会使用其他账号；首次收到的上游 HTTP 错误仍保留其状态码。绑定为 `null`（CLI 的 `--auto`）表示恢复账号池调度。旧 Key 自动使用账号池，无需重新创建。

管理员 HTTP 接口也支持上述操作：

| 接口 | 作用 |
| --- | --- |
| `POST /admin/auth/codex/oauth/complete` | 保存账号，支持可选 `name` |
| `GET /admin/upstreams` | 账号状态、冷却时间与用量；`include_deleted=true` 包含历史记录，不返回凭证 |
| `PATCH /admin/upstreams/{id}` | 修改 `name`、`enabled` 或设置 `reset_cooldown=true` |
| `DELETE /admin/upstreams/{id}` | 退出指定账号，保留历史记录 |
| `POST /admin/keys` | 创建 Key，可选 `upstream_credential_id` |
| `PATCH /admin/keys/{id}/upstream` | 设置 `upstream_credential_id`，传 `null` 取消绑定 |

原有 `auth-status` 和单账号状态接口仍显示最近添加且启用、未退出的账号，查看完整池请使用 `list-upstreams` 或 `/admin/upstreams`。

## 用户、Key 与用量

```bash
docker compose exec looper-ai-gateway python manage.py create-user alice --note 'team-a'
docker compose exec looper-ai-gateway python manage.py create-key --user-id 1 --name alice-dev
docker compose exec looper-ai-gateway python manage.py list-users
docker compose exec looper-ai-gateway python manage.py list-keys
docker compose exec looper-ai-gateway python manage.py disable-key 1
docker compose exec looper-ai-gateway python manage.py enable-key 1
docker compose exec looper-ai-gateway python manage.py delete-key 1
docker compose exec looper-ai-gateway python manage.py usage
```

Key 明文仅在创建时返回，数据库保存 SHA-256 摘要。删除采用软删除：撤销访问、从 Key 列表隐藏并禁止重新启用，保留历史用量。用量汇总包含已删除 Key，计数使用数据库原子累加。

通过鉴权后的请求，包括参数错误和上游错误，都会计入请求次数。流式取消记录为 499；上游未返回的 token 用量无法精确补算。统计用于使用情况追踪，不是账单结算系统。

账号的 `request_count` 表示被调度次数，包含重试和刷新失败；`failure_count` 表示触发冷却的失败次数。下游一次请求只计一次，日志关联最终使用或报错的账号，token 计入该账号。此前失败尝试未返回的用量无法补算。升级前的历史日志保留原数据，不会猜测账号归属；账号维度的统计从升级后开始累计。

管理员 HTTP 接口使用 `Authorization: Bearer <ADMIN_TOKEN>`。接口清单可在 `/docs` 查看；`manage.py` 直接访问数据库，不需要通过 HTTP 管理员鉴权。

## 调用示例

普通文本请求：

```bash
curl http://127.0.0.1:34567/v1/chat/completions \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"hello"}]}'
```

流式请求：

```bash
curl -N http://127.0.0.1:34567/v1/chat/completions \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"hello"}],"stream":true,"stream_options":{"include_usage":true}}'
```

如上游不支持示例模型，请同时调整请求模型与 `DEFAULT_ALLOWED_MODELS`。普通接口失败返回 `error` 对象；超时返回 504，连接失败、畸形数据和缺少终止事件返回 502，上游 HTTP 错误通常保留其状态码。

## 升级、存储与日志

启动服务和执行数据库管理命令时，会自动执行幂等迁移并记录 schema 版本：添加软删除、账号池、Key 绑定及用量归属字段，把旧版上游凭证和 OAuth verifier 转为加密值。现有账号保留并默认启用，新增账号计数初始化为零。升级前先备份数据库。

迁移每批处理 256 条记录，在 Python 中加密后通过 `executemany` 批量写入。未变化的密文和已清理的回调不会重复改写；密钥校验失败会回滚迁移。

上游 access token、refresh token、原始 token JSON 和 PKCE verifier 使用 Fernet 加密。**备份和恢复必须同时保留数据库与原始密钥**；使用环境变量提供密钥时也必须保留该值。更改或丢失密钥后不能解密既有凭证，服务会在迁移检查时拒绝启动。

Fernet 实例按进程与密钥配置缓存，正常加解密不再重复读取密钥文件。运行中替换同一路径的密钥文件不会热加载；密钥配置变更后需重启进程，且已有密文仍需使用原密钥解密。

Compose 将 `./data` 持久化到 `/app/data`，默认密钥文件也在其中。迁移不会清理已有备份或旧版容器日志中的敏感内容。

```bash
docker compose logs --tail=200 looper-ai-gateway
```

新日志只输出事件名、路径、模型、HTTP 状态和上游 request ID，不输出提示词、响应正文、账号 ID 或 token。HTTP 客户端使用连接池并在服务退出时关闭。

## 开发与验证

```bash
pip install -r requirements-dev.txt
python -m pytest -q
ruff check app manage.py tests
ruff format --check app manage.py tests
```

测试使用临时 SQLite 数据库和模拟上游，不读取真实登录态，也不产生模型调用。覆盖错误状态、并发统计、Key 删除、凭证加密、旧数据迁移、跨进程刷新与账号轮换、故障冷却、Key 绑定、账号用量归属、工具与图片转换、SSE 拆包、流式取消和就绪检查。GitHub Actions 在 Python 3.12/3.13 上执行测试和静态检查。
