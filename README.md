# Looper-AI-Gateway

[GitHub 仓库](https://github.com/superikw/Looper-AI-Gateway)

基于 Python / FastAPI 的内部 AI 网关。管理员维护多个 ChatGPT / Codex 上游账号，为下游用户分配独立 API Key，通过 OpenAI 风格接口转发请求并统计用量。

Linux 部署统一使用 **WARP + Gateway**：一个 `docker-compose.yml` 启动两个服务。下游 Key 默认使用上游账号池，也可固定绑定一个账号。每个账号默认 **30 RPM、最大并发 4、最小请求间隔 0**，支持动态修改。

适合内部工具和少量可信用户。当前没有管理 UI、按 Key 配额或跨主机调度。

## 目录

- [功能与兼容范围](#功能与兼容范围)
- [部署](#部署)
- [多账号池与固定绑定](#多账号池与固定绑定)
- [用户与用量管理](#用户与用量管理)
- [配置说明](#配置说明)
- [更新与备份](#更新与备份)
- [常见问题](#常见问题)
- [本地开发与验证](#本地开发与验证)

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

## 部署

以下步骤从首次安装到实际调用依次执行。除 SSH 隧道和浏览器操作外，所有命令均在 **Linux 服务器**执行；下载代码后，所有 `docker compose` 命令均在项目目录执行。使用 Podman 的命令差异见[使用 podman-compose](#使用-podman-compose)。

```text
客户端 → Gateway（服务器 127.0.0.1:34567）→ WARP（容器内 1080）→ 上游
```

WARP 只代理服务端出站流量，所有上游账号共享出口；浏览器登录使用浏览器自己的网络。1080 不映射到宿主机。

### 1. 检查服务器环境

准备 Git、curl、OpenSSL、Docker Engine 和 Compose 插件。Docker 安装请按服务器发行版使用官方步骤：[Ubuntu](https://docs.docker.com/engine/install/ubuntu/)、[Debian](https://docs.docker.com/engine/install/debian/)；已有 Docker 时可单独[安装 Compose 插件](https://docs.docker.com/compose/install/linux/)。宿主机无需安装 Python 或 Codex CLI。

```bash
git --version
curl --version
openssl version
docker --version
docker compose version
docker info
ls -l /dev/net/tun
```

`docker info` 应能连接 Docker 守护进程，`/dev/net/tun` 应存在且为字符设备。以下命令假设当前用户有 Docker 使用权限；如需要，请在 `docker` 命令前加 `sudo`。

如果 TUN 设备不存在，可尝试加载内核模块后重新检查：

```bash
sudo modprobe tun
ls -l /dev/net/tun
```

服务器需允许容器使用 `NET_ADMIN`，Compose 已配置此权限及 TUN 挂载，并按 WARP 上游要求为 Podman 补齐 `MKNOD`、`AUDIT_WRITE`。受限 VPS、LXC 等环境如果仍提示权限不足，需要宿主机或服务商开放对应能力。

### 2. 下载项目并创建配置

```bash
git clone https://github.com/superikw/Looper-AI-Gateway.git
cd Looper-AI-Gateway
cp .env.docker.example .env
```

`cp` 仅首次部署执行。已有部署保留原来的 `.env`、`data` 和 `warp-data`。

### 3. 生成并填写管理员口令

```bash
openssl rand -hex 32
```

保存输出的随机口令，然后编辑配置：

```bash
vi .env
```

将 `ADMIN_TOKEN` 填为生成的口令，确认以下配置。示例中的口令说明文字应替换为真实值：

```dotenv
ADMIN_TOKEN=这里填写刚生成的随机口令
UPSTREAM_AUTH_MODE=oauth_manual
WARP_IMAGE=caomingjun/warp:latest
WARP_SLEEP=5
```

上述命令在宿主机生成 64 个十六进制字符的随机口令，无需启动容器、数据库或 WARP。管理员口令至少 32 个字符，未配置或长度不足时服务会拒绝启动。其余配置可以保持示例默认值，Compose 会自动设置 `UPSTREAM_PROXY_URL=http://warp:1080`。

`ADMIN_TOKEN` 用于管理接口；下游客户端使用第 6 步创建的 `gtw_...` Key。

### 4. 启动服务并验证 WARP

```bash
docker compose build looper-ai-gateway
docker compose up -d
docker compose ps
```

Gateway 会等待 WARP 健康检查通过后启动。正常情况下两个服务都为运行状态，健康状态最终显示 `healthy`。

查看 WARP 实际出口：

```bash
docker compose exec warp curl --noproxy '' --proxy http://127.0.0.1:1080 \
  --fail --silent --show-error --max-time 10 \
  https://cloudflare.com/cdn-cgi/trace
```

输出中应包含独立一行 `warp=on` 或 `warp=plus`。再检查网关进程与配置：

```bash
curl -f http://127.0.0.1:34567/healthz
docker compose exec looper-ai-gateway python manage.py doctor
```

`/healthz` 应返回 `ok: true`。此时尚未登录上游，`/readyz` 返回 503 属于正常情况。如果启动失败，先看日志：

```bash
docker compose logs --tail=100 warp looper-ai-gateway
```

### 5. 登录第一个上游账号

在服务器发起 OAuth 登录：

```bash
docker compose exec looper-ai-gateway python manage.py auth-start
```

输出包含 `session_id`、`auth_url` 和过期时间。记下实际会话 ID，在 **自己电脑的浏览器**打开 `auth_url`，登录希望加入账号池的账号。

登录完成后，浏览器会跳转到类似地址：

```text
http://localhost:1455/auth/callback?code=...&state=...
```

即使页面提示无法连接，也可以复制地址栏中的 **完整 URL**。这是手动回调流程，无需开放服务器 1455 端口，也无需修改默认 `OAUTH_REDIRECT_URI`。

回到服务器，将下面的 `1` 替换为实际 `session_id`，回调地址替换为浏览器中的完整 URL，并保留单引号：

```bash
docker compose exec looper-ai-gateway python manage.py auth-complete \
  --session-id 1 \
  --name account-a \
  --callback-url 'http://localhost:1455/auth/callback?code=实际值&state=实际值'
```

默认授权会话有效期为 15 分钟；过期后重新执行 `auth-start`，使用新会话和新回调 URL 完成登录。

检查账号列表与就绪状态：

```bash
docker compose exec looper-ai-gateway python manage.py list-upstreams
curl -f http://127.0.0.1:34567/readyz
```

正常情况下账号列表包含刚登录的账号，`/readyz` 返回 `ok: true`。该检查不发起模型推理，实际调用在第 7 步验证。

添加更多账号时重复本步骤，并在浏览器切换账号或使用不同浏览器配置。不同 ChatGPT account ID 分别保存；同一未删除账号重新登录会更新凭证并重新启用，保留其限额设置。`--name` 只是可选备注。

### 6. 创建下游用户与 API Key

创建用户：

```bash
docker compose exec looper-ai-gateway python manage.py create-user alice --note team-a
```

使用返回的用户 `id` 创建 Key，下例假设用户 ID 为 `1`：

```bash
docker compose exec looper-ai-gateway python manage.py create-key \
  --user-id 1 --name alice-dev
```

保存输出的 `api_key`，格式为 `gtw_...`。**Key 明文仅在创建时返回**，以后无法从数据库查询原值。默认 Key 自动使用账号池；固定绑定方法见[多账号池与固定绑定](#多账号池与固定绑定)。

### 7. 在服务器验证 API 调用

将示例中的 `你的gtw_key` 替换为刚生成的 Key。示例模型需要在 `DEFAULT_ALLOWED_MODELS` 中，且上游账号有使用权限；白名单不代表模型实时可用。

普通请求：

```bash
curl -sS http://127.0.0.1:34567/v1/chat/completions \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"你好"}]}'
```

流式请求：

```bash
curl -N http://127.0.0.1:34567/v1/chat/completions \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"你好"}],"stream":true,"stream_options":{"include_usage":true}}'
```

Responses 请求：

```bash
curl -sS http://127.0.0.1:34567/v1/responses \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","input":"你好"}'
```

### 8. 从自己电脑访问

默认端口映射为 `127.0.0.1:34567:8080`：服务器宿主机访问 **34567**，容器内监听 **8080**。其他机器无法直接通过服务器公网 IP 访问此端口。

在 **自己电脑的终端**建立 SSH 隧道，替换实际用户名与服务器 IP：

```bash
ssh -N -L 34567:127.0.0.1:34567 用户名@服务器IP
```

保持此 SSH 连接，在客户端填写：

| 客户端配置 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:34567/v1` |
| API Key | 第 6 步创建的 `gtw_...` |
| 模型 | 已通过第 7 步验证的模型 |

如需域名访问，可在服务器配置 Nginx/Caddy HTTPS 反向代理到 `127.0.0.1:34567`。流式接口需关闭代理缓冲，并为长请求设置足够的读取超时。

### 使用 podman-compose

`podman-compose` 与 Docker Compose 的参数及依赖处理随版本不同。例如 [podman-compose 1.0.6](https://github.com/containers/podman-compose/blob/v1.0.6/podman_compose.py) 的 `run` 不接受 `--build`，而且 `run --no-deps` 仍会给临时容器添加 `--requires`。如果 WARP 容器尚未创建，生成口令会因此失败。

使用宿主机 OpenSSL 生成管理员口令，构建和启动分开执行，即可避开这两个问题。

Compose 中的 `sysctls` 使用 `KEY=VALUE` 列表格式，兼容 1.0.6；该版本处理字典格式时会丢失值，导致 `sysctl values must be in the form of KEY=VALUE`。Gateway 健康检查使用 `CMD-SHELL` 保留 `python -c` 的引号。WARP 的 Podman 权限配置参考[上游项目](https://github.com/cmj2002/warp-docker)。

先按第 2 步下载项目、创建 `.env`，然后在项目目录执行：

```bash
podman --version
podman-compose version
openssl rand -hex 32
```

将生成的口令填入 `.env` 的 `ADMIN_TOKEN`，再启动：

```bash
podman-compose build looper-ai-gateway
podman-compose up -d
podman-compose ps
podman-compose logs --tail=100 warp looper-ai-gateway
```

1.0.6 不会按 Docker Compose 的行为等待 `condition: service_healthy`，启动后应按第 4 步通过 Cloudflare trace 确认 WARP 出口可用，再登录账号和调用模型。

后续登录和管理命令，将文中的 `docker compose` 前缀换成 `podman-compose`，例如：

```bash
podman-compose exec looper-ai-gateway python manage.py auth-start
podman-compose exec looper-ai-gateway python manage.py list-upstreams
```

更新镜像与启动也分步执行：

```bash
podman-compose pull warp
podman-compose build looper-ai-gateway
podman-compose up -d
```

所有 Podman 命令保持使用同一用户执行。上述方式解决 CLI 参数差异，WARP 仍需前文的 TUN 与容器权限；Podman 上的真实隧道运行尚未端到端验证。

## 多账号池与固定绑定

以下命令均在服务器的项目目录执行。示例中的账号 ID、用户 ID 和 Key ID 应替换为实际值。

### 查看、暂停和恢复账号

```bash
docker compose exec looper-ai-gateway python manage.py list-upstreams
docker compose exec looper-ai-gateway python manage.py update-upstream 1 --name 'team-a' --disable
docker compose exec looper-ai-gateway python manage.py update-upstream 1 --enable --reset-cooldown
```

未绑定的 Key 按最近使用时间轮换启用、未删除、不在冷却中且有请求名额的账号。选择记录保存在数据库中，同一主机多个 worker 共享调度顺序，重启后也会保留。暂停、删除或冷却中的账号不会接收新的调度；已经发出的请求会继续处理。

OAuth 凭证在到期前 90 秒触发刷新。同一账号通过进程间锁协调刷新，不同账号可并行刷新。同一主机的进程必须共享数据库和密钥目录，SQLite 与这些锁不支持跨主机部署。

### 动态调整请求限额

每个 OAuth 账号默认使用以下限额，已有账号升级后也采用相同默认值：

| 账号配置 | 默认值 | 含义 |
| --- | --- | --- |
| `max_rpm` | 30 | 滚动 60 秒内最多调度 30 次，不在整分钟边界重置 |
| `max_concurrency` | 4 | 最多同时占用 4 个请求名额；流式请求直到上游响应关闭才释放 |
| `min_interval_seconds` | 0 | 默认没有最小间隔，可动态设置非负秒数 |

账号限额是该账号被所有 Key 使用的总和，普通请求、流式请求和固定绑定请求均受约束。每次调度先预约 RPM 和并发名额，实际发送时更新计时，慢刷新不会使预约提前过期。重试、刷新失败和连接失败也会消耗一次调度名额；释放并发不会退还当前窗口内的 RPM。

限额配置持久化在数据库中，可按账号动态更新，无需重启。后续调度和等待中的请求读取最新配置；降低上限不会中断已经获准执行的请求，已有请求结束或当前窗口消退后再接收新请求。重新登录同一未删除账号会保留其限额设置。

```bash
docker compose exec looper-ai-gateway python manage.py update-upstream 1 --max-rpm 30 --max-concurrency 4 --min-interval-seconds 0
docker compose exec looper-ai-gateway python manage.py list-upstreams
```

也可以通过管理员接口修改：

```bash
curl -X PATCH http://127.0.0.1:34567/admin/upstreams/1 \
  -H 'Authorization: Bearer 你的ADMIN_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"max_rpm":30,"max_concurrency":4,"min_interval_seconds":0}'
```

账号池会优先选择有名额的账号；所有候选账号均满额时，最多等待 `UPSTREAM_CAPACITY_WAIT_SECONDS`，仍无名额则返回 429 和 `Retry-After`。固定绑定只等待绑定账号，不会转给其他账号。因本地限额返回的 429 不会触发上游账号冷却；禁用、删除或全部冷却仍返回 503。短时满额不会单独使 `/readyz` 失败。

RPM 历史和并发预约在同一主机的 worker 间共享，重启不会清空最近一分钟的 RPM。正在处理的请求持有独立进程锁，长流式响应不会因固定租期到期而失去名额；进程异常退出后，后续调度会回收其并发名额。已结束且超过一分钟的预约与锁文件会在调度时清理。

### 故障冷却与重试

收到明确的上游 HTTP 401、403、429、500、502、503、504 时，未绑定请求会在尝试次数内改用其他账号，每个账号最多尝试一次。401/403 冷却 300 秒；429 优先遵循 Retry-After（秒数或 HTTP 日期，限制为 1 秒至 1 天），其他错误使用配置的冷却时间。并发错误不会缩短已有冷却；管理员可手动清除。`/readyz` 至少需要一个可用账号，全部冷却时返回 503。

WARP 代理路径上的网络错误不会冷却账号或轮换重试，具体状态见[常见问题](#常见问题)。收到 HTTP 成功响应后，正文错误或流中断不会切号；读写超时也不会自动重放。流式输出始终来自同一个账号。服务端 5xx 重试无法保证上游完全没有处理过前一次请求。账号池不读取上游剩余额度，也不保证不同账号支持完全相同的模型。

### 将 Key 固定绑定到账号

下例新建 Key 或将已有 Key 绑定到账号 `2`，最后一条命令将 Key `1` 恢复为账号池调度：

```bash
docker compose exec looper-ai-gateway python manage.py create-key --user-id 1 --name dedicated --upstream-id 2
docker compose exec looper-ai-gateway python manage.py bind-key 1 --upstream-id 2
docker compose exec looper-ai-gateway python manage.py bind-key 1 --auto
```

固定绑定的账号禁用、删除或冷却时，该 Key 返回 503；请求名额不足并等待超时则返回 429。均不会使用其他账号；首次收到的上游 HTTP 错误仍保留其状态码。绑定为 `null`（CLI 的 `--auto`）表示恢复账号池调度。旧 Key 自动使用账号池，无需重新创建。

### 退出账号

退出指定账号并查看包含历史记录的账号列表：

```bash
docker compose exec looper-ai-gateway python manage.py logout --credential-id 1
docker compose exec looper-ai-gateway python manage.py list-upstreams --include-deleted
```

省略 `--credential-id` 会退出数据库中的所有 OAuth 账号。CLI 和 HTTP logout 都采用软删除，保留加密凭证和历史用量，后续调度、状态查询和刷新排除已删除账号。退出操作会协调登录与刷新锁，避免并发刷新恢复已退出的凭证。

已删除账号重新登录会创建新记录；绑定旧记录的 Key 需要重新绑定。仅暂时停用时使用 `--disable`，之后可用 `--enable` 恢复。文件与环境变量模式的认证需要在各自来源管理。

### 管理员 HTTP 接口

管理员 HTTP 接口也支持上述操作：

| 接口 | 作用 |
| --- | --- |
| `POST /admin/auth/codex/oauth/complete` | 保存账号，支持可选 `name` |
| `GET /admin/upstreams` | 账号状态、冷却时间与用量；`include_deleted=true` 包含历史记录，不返回凭证 |
| `PATCH /admin/upstreams/{id}` | 修改 `name`、`enabled`、`max_rpm`、`max_concurrency`、`min_interval_seconds` 或设置 `reset_cooldown=true` |
| `DELETE /admin/upstreams/{id}` | 退出指定账号，保留历史记录 |
| `POST /admin/keys` | 创建 Key，可选 `upstream_credential_id` |
| `PATCH /admin/keys/{id}/upstream` | 设置 `upstream_credential_id`，传 `null` 取消绑定 |

原有 `auth-status` 和单账号状态接口仍显示最近添加且启用、未退出的账号，查看完整池请使用 `list-upstreams` 或 `/admin/upstreams`。

## 用户与用量管理

```bash
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

## 配置说明

| 配置 | 作用 |
| --- | --- |
| `ADMIN_TOKEN` | 必须手动配置的管理员随机口令，至少 32 个字符 |
| `DATABASE_URL` | 默认 `sqlite+aiosqlite:///./data/gateway.db` |
| `CREDENTIAL_KEY_FILE` | 默认 `./data/credential.key`，首次使用自动生成，文件权限为 0600 |
| `CREDENTIAL_ENCRYPTION_KEY` | 可选 Fernet 密钥；设置后优先于密钥文件，升级时必须保持一致 |
| `UPSTREAM_BASE_URL` | 默认 `https://chatgpt.com/backend-api` |
| `UPSTREAM_AUTH_MODE` | `oauth_manual`、`codex_auth_file` 或 `env_token` |
| `WARP_IMAGE` | WARP 镜像，默认 `caomingjun/warp:latest`，可固定标签或 digest |
| `WARP_SLEEP` | WARP 启动等待秒数，默认 5 |
| `UPSTREAM_TIMEOUT_SECONDS` | 上游 HTTP 超时，默认 120 秒 |
| `UPSTREAM_PROXY_URL` | 可选 HTTP(S) 代理，作用于模型请求和 OAuth 换取/刷新；Docker Compose 固定设置为 `http://warp:1080` |
| `UPSTREAM_MAX_ATTEMPTS` | 账号池每个请求最多尝试的不同账号数，默认 3，范围 1–10；固定绑定只尝试一个 |
| `UPSTREAM_FAILURE_COOLDOWN_SECONDS` | 连接或服务端错误后的冷却秒数，默认 10 |
| `UPSTREAM_RATE_LIMIT_COOLDOWN_SECONDS` | 429 缺少有效 Retry-After 时的冷却秒数，默认 60 |
| `UPSTREAM_CAPACITY_WAIT_SECONDS` | RPM、并发或最小间隔受限时最多等待的秒数，默认 15；设为 0 立即返回 429 |
| `MAX_REQUEST_BYTES` | 请求体大小限制，默认 16777216 字节（16 MiB） |
| `DEFAULT_ALLOWED_MODELS` | 全局模型白名单，留空表示不限制；模型名会规范化 |
| `DEFAULT_INSTRUCTIONS` | 缺少指令时使用的默认提示 |
| `OAUTH_*` | OAuth 地址、客户端标识、回调地址和 PKCE 会话配置 |

模型白名单是访问策略，不能用来发现上游模型。项目没有自动选择“最新模型”的逻辑。

多账号池仅在 `oauth_manual` 模式启用，推荐使用此模式。文件和环境变量模式仍使用单个账号。`codex_auth_file` 从 `CODEX_HOME/auth.json` 读取 access token，文件模式不会自行刷新登录态，需要外部维护；`env_token` 需要设置 `UPSTREAM_BEARER_TOKEN`。所有模式都需要可解析的 ChatGPT account ID，不能用任意官方 API Key 代替。

Docker 镜像未安装 Codex CLI；如使用文件模式，需要挂载认证文件。如使用 CLI 登录管理接口，还需自行准备含 Codex CLI 的运行环境。

`.env.docker.example` 用于 Docker 部署，`.env.example` 用于本地开发。Docker 中 `HOST=0.0.0.0`、`PORT=8080` 是容器内监听地址，宿主机端口在 `docker-compose.yml` 中配置。数据库与密钥文件路径也相对于容器内的 `/app`。

修改 `.env` 后执行 `docker compose up -d` 让配置生效，单独执行 `docker compose restart` 不会重新加载容器环境变量。Compose 在 `environment` 中固定设置上游代理，修改 `.env` 中的 `UPSTREAM_PROXY_URL` 不会覆盖它。账号限额保存在数据库中，通过管理命令或管理员接口修改即可，无需重启。

## 更新与备份

### 数据位置

| 宿主机路径 | 内容 |
| --- | --- |
| `.env` | 管理员口令、运行配置及可能配置的加密密钥 |
| `data/gateway.db` | 用户、Key 摘要、账号凭证、限额与用量 |
| `data/credential.key` | 默认 Fernet 密钥，首次使用自动生成 |
| `data/` 下的其他文件 | SQLite 辅助文件和请求、刷新相关的锁文件 |
| `warp-data/` | WARP 注册与运行数据 |

上游 access token、refresh token、原始 token JSON 和 PKCE verifier 均加密保存。**备份和恢复必须同时保留数据库与对应密钥**；如果设置了 `CREDENTIAL_ENCRYPTION_KEY`，还必须保留该值。丢失或更换密钥后无法解密已有凭证。

### 备份并更新

为了获得一致的 SQLite 备份，先停止服务，停止期间不执行其他管理命令，再归档整个数据目录。以下使用默认目录布局：

```bash
docker compose stop
sudo install -d -m 700 ../looper-backups
sudo tar -czf "../looper-backups/looper-$(date +%Y%m%d-%H%M%S).tar.gz" \
  .env data warp-data
```

确认备份成功后，更新代码和镜像并启动：

```bash
git pull --ff-only
docker compose pull warp
docker compose build looper-ai-gateway
docker compose up -d
docker compose ps
curl -f http://127.0.0.1:34567/readyz
```

再执行一次实际 API 请求验证。更新时保留原 `.env` 和数据目录；新版本增加配置时参考 `.env.docker.example` 补充。之前使用双 Compose 文件的部署，更新后也统一使用单个 `docker-compose.yml`，按上述命令分步构建和启动。

启动服务和执行数据库管理命令时会自动进行幂等迁移，包括旧凭证加密、软删除、账号池及限额字段。迁移按 schema 版本执行，密钥校验失败会回滚。不要把旧代码直接指向已升级数据库作为回滚方案；需要回滚时使用与备份兼容的代码和数据。

### 恢复备份

在项目目录停止服务，将示例归档路径替换为实际备份文件。现有部署先按上面的步骤另行备份，并移走原数据目录，避免残留 SQLite 辅助文件混入恢复的数据。

```bash
docker compose down
sudo install -d -m 700 ../looper-backups
if [ -d data ]; then
  sudo mv data "../looper-backups/data-before-restore-$(date +%Y%m%d-%H%M%S)"
fi
if [ -d warp-data ]; then
  sudo mv warp-data "../looper-backups/warp-data-before-restore-$(date +%Y%m%d-%H%M%S)"
fi
sudo tar -xzf /绝对路径/looper-backups/looper-备份时间.tar.gz -C "$PWD"
```

确认解压成功后再启动并验证：

```bash
docker compose build looper-ai-gateway
docker compose up -d
docker compose ps
curl -f http://127.0.0.1:34567/readyz
```

如果恢复了旧版本的备份，先确认当前代码可以迁移该版本的数据。自定义数据库位置或密钥来源时，需要另外备份并恢复对应内容。

### 日常命令

```bash
# 跟踪两个服务的日志，Ctrl+C 结束查看
docker compose logs -f --tail=100 warp looper-ai-gateway

# 停止服务，保留数据
docker compose stop

# 按当前配置启动或更新容器
docker compose build looper-ai-gateway
docker compose up -d

# 停止并删除容器及默认网络，保留宿主机数据目录
docker compose down
```

日志输出事件名、路径、模型、HTTP 状态和上游 request ID，不输出提示词、响应正文、账号 ID 或 token。迁移不会清理已有备份或旧版日志中的敏感内容。

## 常见问题

### Podman 报 sysctl 缺少 KEY=VALUE

如果启动日志是 `--sysctl net.ipv6.conf.all.disable_ipv6`，缺少末尾的 `=0`，说明旧版 provider 把字典中的值丢掉了。项目已改为以下列表格式，更新代码后重新启动即可：

```yaml
sysctls:
  - net.ipv6.conf.all.disable_ipv6=0
  - net.ipv4.conf.all.src_valid_mark=1
```

后续的 `WARP container not found` 或依赖不存在报错是 WARP 创建失败的连带结果，先处理最早出现的 sysctl 错误。保留现有 `.env`、`data` 和 `warp-data`。

### Podman 生成管理员口令时报 WARP 依赖不存在

如果 `podman-compose run --no-deps` 仍生成 `--requires=looper-ai-gateway_warp_1`，并报 `no such container`，这是 1.0.6 的依赖处理行为。无需创建 WARP 容器来生成口令，直接在宿主机执行：

```bash
openssl rand -hex 32
```

将输出写入 `.env` 的 `ADMIN_TOKEN`，然后按前文构建并启动。`podman compose` 是调用外部 Compose provider 的入口；当它使用 `/usr/bin/podman-compose` 时，也受该 provider 版本行为影响。

### WARP 未就绪，Gateway 无法启动

```bash
docker compose ps
docker compose logs --tail=100 warp
docker compose exec warp sh /usr/local/bin/warp-healthcheck.sh
```

手动健康检查成功时无输出且退出码为 0。健康检查会通过 HTTP 代理访问 Cloudflare trace，仅在实际出口为 `warp=on` 或 `warp=plus` 时通过。检查 TUN、`NET_ADMIN` 权限和服务器出站网络；WARP 运行配置参考 [warp-docker 项目](https://github.com/cmj2002/warp-docker)。

Gateway 只在启动时等待 WARP 健康。WARP 运行中变为 unhealthy 不会自动停止或重启 Gateway，需结合日志与实际代理请求判断。

### 健康检查正常，但无法调用模型

| 检查 | 验证范围 |
| --- | --- |
| `/healthz` | Gateway 进程存活 |
| `/readyz` | 数据库与至少一个可用上游账号；必要时刷新凭证 |
| WARP trace | 代理路径可用，出口经过 WARP |
| 实际模型请求 | 账号权限、模型、剩余额度及完整转发路径 |

`/readyz` 不发起推理请求。未登录、账号全部禁用或退出、账号全部处于冷却、凭证不可用时可能返回 503；短时 RPM 或并发满额不会单独使其失败。

### 请求返回 429、503 或 504

| 情况 | 行为与排查方向 |
| --- | --- |
| 本地账号限额耗尽 | 最多等待 15 秒，仍无名额返回 429 和 `Retry-After`；查看账号 RPM、并发和最小间隔 |
| 账号全部不可用或绑定账号不可用 | 返回 503；检查账号是否禁用、退出、冷却或需要重新登录 |
| WARP 代理路径连接失败 | 返回 503；检查 WARP 状态与日志 |
| WARP 代理路径超时 | 返回 504；检查网络和 `UPSTREAM_TIMEOUT_SECONDS` |
| 上游明确返回 HTTP 错误 | 通常保留最终上游状态码，按账号池规则决定是否切换账号 |
| 上游正文畸形或缺少终止事件 | 通常返回 502；流式开始后通过 SSE 错误事件报告 |

WARP 不可用时不会自动回退直连。明确的代理错误，以及配置共享代理后无法归属账号的网络错误，不会冷却或轮换账号重试。流式请求开始输出后的故障通过 SSE 报错，并记录失败状态。

### OAuth 回调失败

确认 `auth-complete` 使用当前会话的 ID、浏览器中的完整回调 URL，URL 用单引号包住以保留 `&`。授权会话默认 15 分钟过期，state 必须匹配；过期或已完成时重新发起登录。浏览器打不开 localhost 回调页面不会妨碍复制 URL。

### 修改配置后没有生效

运行参数在 `.env` 中修改后执行 `docker compose up -d`，端口映射在 `docker-compose.yml` 中修改。账号 RPM、并发和最小间隔用 `update-upstream` 动态修改。运行中替换同路径的密钥文件不会热加载，也不能直接用于解密旧密文。

## 本地开发与验证

部署使用前文的 WARP + Gateway。本节用于 Linux/macOS 的源码开发，要求 Python 3.12 或更新版本；进程间锁依赖 POSIX 文件锁。Windows 用户可连接 Linux Docker 主机部署。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python manage.py generate-admin-token
```

把生成的口令写入 `.env` 的 `ADMIN_TOKEN` 后启动：

```bash
python -m app.main
```

本地默认地址为 `http://127.0.0.1:8080`。本地管理命令使用 `python manage.py ...`，不加 `docker compose exec looper-ai-gateway` 前缀；HTTP 示例端口相应改为 8080。已有 `.env` 时保留原配置。

验证命令：

```bash
python -m pytest -q
ruff check app manage.py tests
ruff format --check app manage.py tests
```

测试使用临时 SQLite 数据库和模拟上游，不读取真实登录态，也不产生模型调用。覆盖凭证加密、迁移、软删除、账号池与限额、动态配置、Key 绑定、账号用量归属、工具与图片转换、SSE、共享代理故障及就绪检查。代理测试使用本地代理服务器，不依赖真实 WARP。GitHub Actions 在 Python 3.12/3.13 上执行测试和静态检查，并校验 WARP + Gateway 的 Compose 配置。
