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
- [模型黑名单](#模型黑名单)
- [配置说明](#配置说明)
- [更新与备份](#更新与备份)
- [常见问题](#常见问题)
- [本地开发与验证](#本地开发与验证)

## 功能与兼容范围

| 入口 | 支持范围 |
| --- | --- |
| `POST /v1/chat/completions` | 文本、用户图片、函数工具及工具结果、普通 JSON 和 SSE 流式输出 |
| `POST /v1/responses` | Responses 输入、工具定义、普通 JSON 和具名 SSE 事件 |
| `GET /v1/models` | 返回本地展示目录并过滤黑名单；目录外的模型也可请求，不代表上游实时可用模型 |
| `POST /v1/images/generations` | 图像生成，透传到 Codex Images API |
| `POST /v1/images/edits` | 图像编辑，原始请求透传到 Codex Images API |
| `/v1/*` 其他路由 | API Key 鉴权后按原始方法、请求体和查询参数透传 |
| `/admin/*` | 用户、Key、上游账号池、模型黑名单、OAuth 登录和用量管理 |
| `GET /healthz` | 进程存活检查 |
| `GET /readyz` | 数据库和上游凭证就绪检查；必要时刷新凭证，不发起模型推理 |

Chat Completions 支持 `tools` / `tool_choice`、`parallel_tool_calls`、`temperature`、`reasoning` / `reasoning_effort`、`metadata`、`response_format`（text / json_object / json_schema）。`max_tokens`、`max_completion_tokens` 和 `max_output_tokens` 均映射为上游输出 token 上限，同一请求只能指定一个。网关会保留并透传客户端新增的顶层字段（例如 `prompt_cache_key`、`client_metadata` 和未来字段），不在本地维护完整的上游字段白名单；最终是否支持由上游返回结果决定。

图像生成使用 `POST /v1/images/generations`，请求体遵循 OpenAI Images API（例如 `model`、`prompt`、`size`、`quality`、`n`），网关会将其转发到 Codex Images API 的 `/codex/images/generations`。`/v1/*` 的其他方法和路径在 API Key 鉴权后保留原始请求体与查询参数转发到对应的 Codex 路径；是否实际可用由上游账号和接口能力决定。

默认 `RESPONSES_PASSTHROUGH=true`：原生 Responses 保留请求正文、模型名、未知字段、方法和查询参数，不强制修改 `stream`、`store`、instructions 或 include。响应按收到的正文增量转发，保留 SSE 心跳、`event/id/retry` 字段、未知事件、顺序及完成事件后的尾部数据；解析只旁路观察错误和用量，不重建正常事件。保留端到端请求/响应头，排除逐跳头、客户端鉴权、Cookie 和上游 Set-Cookie，并使用所选账号的上游凭证。HTTP 库处理压缩传输，转发解压后的正文时移除原 Content-Encoding 和 Content-Length，避免重复解压。其他原始透传路由也增量转发响应。

原生模式不把 HTTP 200 内的业务失败改写为 HTTP 502：客户端收到原始状态与错误正文/事件；日志分别记录上游 HTTP 状态及观察到的业务失败。传输中断后关闭连接，不伪造成功终止事件。Chat Completions 保留独立的协议适配层，以 `[DONE]` 结束，可通过 `stream_options.include_usage=true` 获取最终用量。需要旧版 Responses 协议转换时设置 `RESPONSES_PASSTHROUGH=false`；该模式会补齐/转换字段、重组事件，并将部分业务失败转换成 502。

用量观察对单个尚未结束的事件或 JSON 正文最多保留 1 MiB；超限时继续原样转发，无法观测的用量不推算，也不会仅凭观察上限把请求判为上游失败。此上限不限制实际响应正文大小。

Chat 及旧版 Responses 适配层不支持的参数和内容类型会返回 400，包括多候选结果（`n > 1`）、音频和 `store=true`；缺少指令时补充 `DEFAULT_INSTRUCTIONS`。原生 Responses 将这些字段交由上游处理，不保证上游支持任意参数。Codex CLI/Desktop 兼容范围是 HTTP Responses + SSE，未实现 WebSocket；模型与参数支持取决于账号和 Codex backend，实际客户端版本仍需端到端验收。

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

Gateway 会在 WARP 容器创建后启动，不会因为 WARP 首次注册或健康检查长时间处于 `starting` 而阻塞 Compose。正常情况下两个服务都应运行，健康状态最终显示 `healthy`；WARP 就绪前发送上游请求会返回代理不可用错误。

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

将示例中的 `你的gtw_key` 替换为刚生成的 Key。网关默认允许所有模型，仅拦截数据库黑名单中的模型；上游账号仍需有使用权限。

以下示例使用 `gpt-6.1-sol`。[OpenAI 官方说明](https://learn.chatgpt.com/docs/enterprise/workspace-model-availability#prepare-for-the-gpt-55-retirement)指出，`gpt-5.5` 已于 2026 年 10 月 14 日从 ChatGPT 登录的 Codex 中退役，API Key 认证不受该次退役影响。实际可用模型仍取决于登录账号和工作区权限。

普通请求：

```bash
curl -sS http://127.0.0.1:34567/v1/chat/completions \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol","messages":[{"role":"user","content":"你好"}]}'
```

流式请求：

```bash
curl -N http://127.0.0.1:34567/v1/chat/completions \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol","messages":[{"role":"user","content":"你好"}],"stream":true,"stream_options":{"include_usage":true}}'
```

Responses 请求：

```bash
curl -sS http://127.0.0.1:34567/v1/responses \
  -H 'Authorization: Bearer 你的gtw_key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol","input":"你好"}'
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

启动后应按第 4 步通过 Cloudflare trace 确认 WARP 出口可用，再登录账号和调用模型。Compose 保留 WARP 健康检查用于状态显示，但不把 Gateway 启动绑定到该条件，避免首次注册或 Podman 健康检查卡住整个启动命令。

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

换号之间执行递增退避并添加少量随机延迟：默认基础 0.25 秒、单次最多 2 秒、累计退避等待最多 5 秒；`UPSTREAM_MAX_ATTEMPTS=3` 包含首次请求，因此最多重试两次。有效 `Retry-After` 是最短等待时间，若它超过单次/累计等待上限则停止重试并保留原错误，不提前重试。累计上限限制退避等待，不是整条生成请求的总时限。明确的额度耗尽、计费或账号停用错误不自动换号；共享代理传输错误也不换号。Codex 自身可能重试，需要一并评估，网关上限不能限制客户端另起的新请求。

请求含 `previous_response_id` 或 `conversation` 时禁用跨账号重试。依赖服务端续接状态时应固定绑定原账号；当前没有自动跨请求账号亲和映射。无状态的完整历史重放不受此限制。原生 HTTP 错误正文同样有字节和等待上限，无法在边界内完成检查时开始正常转发，不再透明换号。

上游 HTTP 200 不代表生成成功。OAuth 账号池对 `/v1/responses` 和 `/v1/chat/completions` 的 SSE 响应先进行有界预读取：默认最多 8 个数据事件、65536 字节、1 秒，任一阈值达到后结束检查。只允许缓冲空的 `response.created` / `response.in_progress` 生命周期事件；观察到文本、拒绝、工具调用、其他输出或未知事件后立即结束检查。成功时按原顺序消费缓冲内容，事件不丢失、不重复。

上游缺少 `Content-Type` 时也在同一预读取预算内识别正文格式，兼容 SSE 和普通 JSON，保留已读字节。JSON 使用独立业务错误分类，不误当作 SSE 解析；若直到预读取边界之后才识别出正文格式，继续正常消费，不重新开启重试窗口。两种下游接口的流式与非流式请求均适用。

只有在上述边界内遇到明确早期失败，且错误代码在 `overloaded_error`、`server_overloaded`、`server_is_overloaded`、`unavailable`、`service_unavailable`、`server_unavailable`、`temporarily_unavailable` 白名单中，或错误消息完整匹配 `Our servers are currently overloaded. Please try again later.` / `The service is temporarily unavailable. Please try again later.`，才允许切换账号。`server_is_overloaded` 不依赖错误文案，兼容其伴随类型 `service_unavailable_error`；不将该类型下的任意未知错误一律重试。相同的明确 JSON 业务失败也可在有界检查完成后切号；普通 JSON 成功响应与其他错误不因此重试。失败响应自身包含输出时也不重放。HTTP 错误、可重试连接失败和早期业务失败共用 `UPSTREAM_MAX_ATTEMPTS` 和已尝试账号集合；固定 Key 不跨账号，用户路由限制继续生效。每次切换前关闭旧连接并释放并发租约，已经消耗的 RPM 不退还。无备用账号或次数耗尽时保留最后有价值的上游错误及请求 ID。

下游响应开始、预读取达到阈值或已观察到有意义输出后，不再透明切号。非流式下游请求也遵守相同的上游输出边界。其他原始透传接口、图像生成及编辑不增加 SSE 重试。读写故障不重放；只有建立连接阶段的特定错误可按已有预算切号。重试不能保证前一个账号没有计算或计费，每次上游明确返回的用量只统计一次，失败尝试未返回的用量无法补算。

`overloaded` 不再被预设为所有账号都会遇到的共享故障，但本版仍不据此执行长期账号级冷却；HTTP 与 SSE 中的上述白名单错误采用相同规则，401/403 继续账号级冷却。“账号＋模型”的短窗口失败、避让和限量探测恢复尚未实现，作为独立改动交付；本版不保证调度优先避开历史 overloaded 账号。账号池也不保证不同账号支持完全相同的模型。

WARP 代理请求路径上的传输错误不会冷却账号或轮换重试，具体状态见[常见问题](#常见问题)。`upstream proxy transport is unavailable` 仅表示配置共享代理的请求路径发生故障，不能证明 WARP 本身故障；结合尝试记录中的连接、读取或协议异常类型定位。

### 请求与尝试诊断

每个 `/v1/` 用户请求生成短 `x-gateway-request-id`，不采用客户端传入的值。同一次用户请求的全部上游尝试共用该 ID，每次尝试单独写一条请求日志，`retry_count` 从 0 递增。后台和控制台显示 `a1b2c3d4-e5f60718/0`、`a1b2c3d4-e5f60718/1`，并单独保留每次的上游请求 ID、HTTP 状态、业务结果、耗时和账号；本地拒绝记录没有上游 HTTP 状态。重试失败日志在等待下一账号前即写入，最终结果不重复生成同一条上游日志。

用户请求计数和仪表盘请求数只累计一次，用量和费用累计每次上游明确返回的用量，未返回的用量不推算。后台“查看尝试”可还原完整链路；已登录管理员也可查询 `/api/logs/{log_id}/attempts`。历史日志与原 ID 不回写，未记录过的重试次数保持未知。每次尝试同时输出一行 `event=upstream_attempt` 的 JSON，字段包括：

```text
gateway_request_id, display_request_id, retry_count, attempt, upstream_credential_id, path, model,
upstream_http_status, upstream_request_id, failure_category,
upstream_error_type, upstream_error_code, transport_exception_type,
retry_decision, retry_reason, preview_stop_reason,
response_headers_ms, preview_elapsed_ms, failure_event_ms, elapsed_ms
```

`retry_reason` 说明换号或停止重试的原因，`preview_stop_reason` 单独说明预读取为何结束。两者可以不同，例如带会话续接字段时，即使早期识别到可重试错误，也会保留 `stateful_request` 和 `retryable_failure`。后台“查看尝试”同时展示中文含义和原始代码。

| 常见原因代码 | 含义 |
| --- | --- |
| `preview_timeout` / `preview_event_limit` / `preview_byte_limit` | 预读取达到时间 / 事件数 / 字节数上限，进入正常转发 |
| `meaningful_output` / `unknown_event` | 已观察到输出、工具调用或未知事件，停止预读取并禁止重放 |
| `stateful_request` / `bound_account` | 会话续接请求 / 固定绑定 Key，不跨账号 |
| `non_retryable_error` | 错误不符合白名单，或属于永久错误 |
| `attempt_limit` | 达到总尝试次数上限 |
| `retry_after_exceeds_limit` / `backoff_budget_exhausted` | 上游要求的等待超过单次上限 / 累计退避预算不足 |
| `early_retry_disabled` | 配置关闭早期业务失败重试 |
| `no_available_account` / `scheduler_timeout` | 没有可用备用账号 / 等待备用账号超时 |
| `retryable_business_error` / `retryable_http_error` | 早期业务失败 / HTTP 错误触发换号计划 |

`response_headers_ms` 从本次尝试开始计时，包含取得凭证、容量准入及等待响应头的耗时；`preview_elapsed_ms` 仅计预读取本身；`failure_event_ms` 从收到响应头起计，表示网关第一次识别失败的时间，而非上游生成该事件的时间。流结束后才判定的缺少终止事件不伪造失败事件时间。历史记录与未观察到的字段保持空。若决定换号后调度发现无备用账号，数据库更新该次的最终处理原因为 `no_available_account`，控制台另输出 `upstream_retry_stopped` 元数据事件，不新增上游请求或用量日志。

预读取窗口仍默认为 1 秒，不因单次请求总耗时超过 1 秒就扩大。先结合 `preview_stop_reason=preview_timeout` 与 `failure_event_ms` 确认失败是否通常晚于窗口，再按需要将 `SSE_PREVIEW_MAX_SECONDS` 调至有界的 3–5 秒；没有输出时等待首批数据的时间可能随之增加。出现输出或未知事件仍立即结束预读取，禁止已输出内容的透明重放。

| `failure_category` | 含义 |
| --- | --- |
| `upstream_http` | 上游 HTTP 错误 |
| `sse_business` | HTTP 成功后的 SSE 业务失败 |
| `upstream_business` | HTTP 成功后的 JSON 业务失败 |
| `missing_terminal` | 流结束时缺少终止事件 |
| `proxy_transport` / `network_transport` | 代理请求路径 / 非代理路径传输故障 |
| `local_capacity` | 网关本地容量或账号调度不可用 |
| `client_cancelled` | 客户端取消，记账 499 并继续传播取消 |
| `gateway_internal` | 网关内部异常，记账 500 并继续传播异常 |
| `sse_protocol` | 上游响应格式或协议错误 |

成功尝试的失败分类为空。诊断只保存元数据，错误类型/代码和请求 ID 受格式限制，不记录令牌、Authorization、Cookie、完整请求正文或对话内容。内部异常在服务端记录异常类型、堆栈位置及网关请求 ID，对客户端仅返回通用错误；已经开始的流无法再改变 HTTP 状态。尝试记录与请求日志使用同一保留天数，清理日志时一并清理。

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

### 独立管理后台

Compose 会额外启动 `looper-ai-admin`，默认映射到宿主机 `0.0.0.0:34568`，不会占用 API 的 34567 端口。首次部署后在服务器本地设置超级管理员：

```bash
docker compose build looper-ai-gateway looper-ai-admin
docker compose up -d
docker compose exec -T looper-ai-admin python manage.py set-superadmin --username root
```

默认直接访问 `http://服务器IP:34568/`，不需要 HTTPS 或反向代理。`ADMIN_REQUIRE_HTTPS=false` 为默认值；设置为 `true` 时才要求远程后台 API 使用 HTTPS（HTTP 返回 426），本机访问仍允许 HTTP。HTTPS 可由后台直接提供，也可以通过反向代理提供。登录只通过 POST 提交，密码不进入 URL，服务端使用加盐 PBKDF2 哈希；后续认证使用 HttpOnly 会话 Cookie，HTTPS 下带 Secure。HTTP 连接本身不加密传输；启用 HTTPS 后才有链路加密。

**可选：没有反向代理时直接开启 HTTPS**

在服务器项目目录执行（将 `服务器IP` 替换为浏览器访问的实际 IP；使用域名则填写域名；可重复 `--host` 添加多个地址）：

```bash
docker compose build looper-ai-admin
docker compose run --rm --no-deps looper-ai-admin python manage.py generate-admin-cert --host 服务器IP
```

如需启用 HTTPS，在 `.env` 设置容器内的证书路径，并开启 HTTPS 要求：

```dotenv
ADMIN_REQUIRE_HTTPS=true
ADMIN_SSL_CERTFILE=/app/data/admin-tls/cert.pem
ADMIN_SSL_KEYFILE=/app/data/admin-tls/key.pem
```

```bash
docker compose up -d --build looper-ai-admin
```

打开 `https://服务器IP:34568/`。同一管理端口会改为 HTTPS，不能继续用 HTTP；API 的 34567 端口不受影响。证书和私钥存放在已挂载的 `./data/admin-tls/`，生成命令不会覆盖已有文件，私钥权限为 0600。自签证书有效期一年，浏览器会提示不受信任；确认服务器证书后导入信任，或使用受信 CA 签发的证书替换。正式证书同样通过以上两个变量配置，证书更新后重启后台。

**使用反向代理或 SSH 隧道**

反向代理需转发 `Host` 和 `X-Forwarded-Proto: https`，并在管理容器中通过 `FORWARDED_ALLOW_IPS` 配置可信代理 IP；只信任实际代理地址，避免客户端伪造协议。也可以建立 SSH 隧道：

```bash
ssh -N -L 34568:127.0.0.1:34568 用户名@服务器IP
```

隧道建立后访问 `http://127.0.0.1:34568/`。若容器网络将本机请求识别为远程地址，可在仅允许隧道访问的部署中设置 `ADMIN_REQUIRE_HTTPS=false`，或继续使用 HTTPS 代理。

超级管理员可以创建管理员。新管理员首次登录必须修改自动生成的初始密码；超级管理员重置密码会立即撤销该管理员的所有会话。普通用户创建时同时生成 API Key，明文 Key 只显示一次。

后台按页面划分为仪表盘、用户管理、请求日志、模型价格、上游账户和设置，各页面支持直接打开 URL。仪表盘提供今日、昨日、本周和本月用量以及 CPU、内存、磁盘监控。数据库统一保存 UTC，API 时间附 UTC 标记；活动会话、请求日志、管理员创建时间、价格更新时间、账户过期与额度重置等显示统一使用设置中的 IANA 时区，例如 `Asia/Shanghai`。统计边界也使用该时区。 请求日志和最近请求显示原始接口类型及路径（Responses / Chat Completions）。仪表盘会话列表每 3 秒独立刷新，区分进行中与最近 5 分钟已结束的请求，会话计数只统计进行中请求；透传接口同样记录会话。用户/上游双向路由分配列表均支持“全选”和“反选”，点击保存后生效。

上游账户页可以通过 OAuth 动态添加账户，删除、启停账户，修改 RPM、并发数、最小请求间隔和清除失败冷却。用户页可为用户多选上游账户，账户页可为账户多选用户；两侧编辑同一份路由。新用户和新账户默认无路由，无路由用户请求返回 503，无路由账户不参与这些用户的调度。多对多路由需要 `UPSTREAM_AUTH_MODE=oauth_manual`；单 Key 的旧绑定会进一步限制在用户的已分配账户范围内。升级会一次性将原有用户与原有账户池建立路由，保留旧 Key 绑定限制；后续新增项不再自动分配。

账户显示名称依次采用：自定义备注、上游邮箱、上游名称、上游账户 ID、本地账户编号。邮箱/上游名称仅在 OAuth 响应或令牌声明中实际返回时提取；重新授权不会覆盖已有自定义备注，维护界面可清空备注以恢复自动显示。旧账户没有提取过身份字段时会先使用账户 ID，重新绑定后保存可获取的身份字段。日志显示实际请求的上游账户名称、本地编号，并返回上游账户 ID；未选中上游的请求显示“—”。删除账户保留历史日志关联。

用户管理的“请求限制”可设置下游用户的最大并发、RPM（滚动 60 秒，0 表示不限）和最小请求间隔。新旧用户默认并发均为 2，RPM 和最小间隔默认不限制；同一用户的所有 API Key 共用额度，多个网关进程共享计数。下游超限立即返回 HTTP 429 与 `Retry-After`，不在网关排队；被拒绝的请求会记入日志，但不占用名额。并发名额覆盖完整请求，包括等待上游空位、流式输出与清理过程，完成/断开/取消后释放；工作进程退出遗留的名额会在下一次准入时回收。

上游账户池的限制独立生效：调度会尝试用户已分配的可用账户，全部繁忙时按 `UPSTREAM_CAPACITY_WAIT_SECONDS`（默认 15 秒）等待；到期仍超限则返回 429 和 `Retry-After`。等待不是持久队列，也不保证严格 FIFO；无可用路由/账户返回 503。客户端收到 429 后可按 `Retry-After` 延迟重试。

账户状态包括凭证过期、失败冷却、累计请求与 token、失败数和进行中请求数。点击“刷新额度”会查询上游 `/wham/usage`，显示主/次额度窗口已用百分比、重置时间和快照更新时间。上游未提供的项目显示“未提供”，查询失败保留上次成功快照；本地累计用量与上游套餐额度分别展示。

新请求记账时，若模型尚未定价，会精确匹配 [LiteLLM 价格目录](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json)，优先使用 OpenAI / Anthropic 等原厂 provider 条目，之后回退到 [OpenRouter 模型目录](https://openrouter.ai/api/v1/models)。不会用 Azure、Bedrock 等其他部署的价格代替原厂，也不会猜测相似模型、内部别名或自动去掉版本号。外部目录每 token 的美元价格统一换算为 USD / 百万 tokens；目录成功缓存一小时，失败一分钟后重试，最多并行等待约 3 秒，网络失败仍保留请求用量记录。

模型价格页可以手动设置输入、输出和缓存读取价格，查看来源与更新时间。手动配置以及已有价格不会被自动覆盖；官方报价可对照 [OpenAI](https://developers.openai.com/api/docs/pricing) 和 [Claude](https://platform.claude.com/docs/en/about-claude/pricing)。这里的自动原厂价来自 LiteLLM 的原厂条目，并非实时抓取官方 HTML 价目表。未匹配模型在价格页列为待设置，费用暂记 `$0`；补价仅影响后续请求，不会回写历史账单。

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

账号的 `request_count` 表示被调度次数，包含重试和刷新失败；`failure_count` 表示触发冷却的失败次数。下游一次请求只计一次，每次上游尝试均有独立日志并关联实际账号，明确返回的 token 分别计入相应账号。失败尝试未返回的用量无法补算。升级前的历史日志保留原数据，不会猜测账号归属或重试次数。

管理员 HTTP 接口使用 `Authorization: Bearer <ADMIN_TOKEN>`。接口清单可在 `/docs` 查看；`manage.py` 直接访问数据库，不需要通过 HTTP 管理员鉴权。

## 模型黑名单

默认黑名单为空，所有模型名都允许转发，无需预先登记。黑名单对所有 Key、上游认证模式和账号生效，普通请求与流式请求使用相同检查。命中时返回 HTTP 403 和 `model blocked: <模型名>`，记录拒绝日志，不向上游发出请求。

黑名单持久化在数据库中，可动态添加、查看和移除。提交后从下一次请求开始生效，无需重启；已经开始的请求继续执行。模型名按请求相同规则规范化后精确匹配，例如 `GPT6.1_sol` 和 `gpt-6.1-sol` 视为同一模型，不支持通配符。重复添加或移除均可安全执行。

在服务器项目目录执行。以下为 Podman 命令，Docker 用户将 `podman-compose` 换成 `docker compose`：

```bash
# 添加黑名单
podman-compose exec looper-ai-gateway python manage.py block-model gpt-6.1-sol

# 查看黑名单
podman-compose exec looper-ai-gateway python manage.py list-blocked-models

# 从黑名单移除，恢复转发
podman-compose exec looper-ai-gateway python manage.py unblock-model gpt-6.1-sol
```

也可以使用管理员接口：

```bash
curl -sS -X POST http://127.0.0.1:34567/admin/models/blacklist \
  -H 'Authorization: Bearer 你的ADMIN_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol"}'

curl -sS http://127.0.0.1:34567/admin/models/blacklist \
  -H 'Authorization: Bearer 你的ADMIN_TOKEN'

curl -sS -X DELETE --get http://127.0.0.1:34567/admin/models/blacklist \
  -H 'Authorization: Bearer 你的ADMIN_TOKEN' \
  --data-urlencode 'model=gpt-6.1-sol'
```

`MODEL_CATALOG` 仅配置 `GET /v1/models` 的展示列表，默认展示 `gpt-6.1-sol`，并自动过滤黑名单。目录之外的模型仍可直接请求；留空只会返回空目录，不会禁用模型。该目录不查询上游实时可用性。

升级时自动创建空黑名单，原有用户、Key 和账号不变。旧的 `DEFAULT_ALLOWED_MODELS` 配置不再生效，可以从 `.env` 删除；如需保留客户端展示列表，改用 `MODEL_CATALOG`。原来未列入白名单的模型升级后默认放行，需要禁用的模型请主动加入黑名单。

## 配置说明

| 配置 | 作用 |
| --- | --- |
| `ADMIN_TOKEN` | 必须手动配置的管理员随机口令，至少 32 个字符 |
| `ADMIN_HOST_PORT` | 管理后台宿主机端口，默认 34568；Compose 端口映射使用 |
| `ADMIN_BIND_ADDRESS` | 管理后台绑定地址，默认 0.0.0.0 |
| `ADMIN_PORT` | 管理容器内部端口，默认 8081 |
| `ADMIN_REQUIRE_HTTPS` | 默认 false，允许 HTTP；设为 true 才要求远程后台 API 使用 HTTPS |
| `ADMIN_SSL_CERTFILE` | 后台直接提供 HTTPS 的 PEM 证书/证书链路径（Compose 使用容器内路径） |
| `ADMIN_SSL_KEYFILE` | 对应 PEM 私钥路径；必须与证书同时配置 |
| `DATABASE_URL` | 默认 `sqlite+aiosqlite:///./data/gateway.db` |
| `CREDENTIAL_KEY_FILE` | 默认 `./data/credential.key`，首次使用自动生成，文件权限为 0600 |
| `CREDENTIAL_ENCRYPTION_KEY` | 可选 Fernet 密钥；设置后优先于密钥文件，升级时必须保持一致 |
| `UPSTREAM_BASE_URL` | 默认 `https://chatgpt.com/backend-api` |
| `TIMEZONE` | 统计默认时区；留空时使用服务器本机时区 |
| `LOG_RETENTION_DAYS` | 请求日志保留天数，默认 7 |
| `UPSTREAM_AUTH_MODE` | `oauth_manual`、`codex_auth_file` 或 `env_token` |
| `WARP_IMAGE` | WARP 镜像，默认 `caomingjun/warp:latest`，可固定标签或 digest |
| `WARP_SLEEP` | WARP 启动等待秒数，默认 5 |
| `UPSTREAM_TIMEOUT_SECONDS` | 上游 HTTP 超时，默认 120 秒 |
| `UPSTREAM_PROXY_URL` | 可选 HTTP(S) 代理，作用于模型请求和 OAuth 换取/刷新；Docker Compose 固定设置为 `http://warp:1080` |
| `UPSTREAM_MAX_ATTEMPTS` | 账号池每个请求最多尝试的不同账号数，默认 3，范围 1–10；固定绑定只尝试一个 |
| `RESPONSES_PASSTHROUGH` | 默认 true，原生 Responses 正文与 SSE 增量透传；false 使用旧版协议适配层 |
| `UPSTREAM_RETRY_BASE_DELAY_SECONDS` | 换号退避基础延迟，默认 0.25 秒，范围 0–10 |
| `UPSTREAM_RETRY_MAX_DELAY_SECONDS` | 单次退避等待上限，默认 2 秒，范围 0–60 |
| `UPSTREAM_RETRY_MAX_WAIT_SECONDS` | 同一次用户请求累计退避等待上限，默认 5 秒，范围 0–120 |
| `SSE_EARLY_RETRY` | 默认 true，OAuth 池启用有界早期 SSE/JSON 白名单业务失败重试；false 可关闭新增预读取重试 |
| `SSE_PREVIEW_MAX_EVENTS` | 默认 8，范围 1–64；达到数据事件数后停止预读取 |
| `SSE_PREVIEW_MAX_BYTES` | 默认 65536，范围 1024–262144，必须是 1024 的整数倍 |
| `SSE_PREVIEW_MAX_SECONDS` | 默认 1，范围大于 0 且不超过 5 秒；超时保留正在读取的任务供正常转发 |
| `UPSTREAM_FAILURE_COOLDOWN_SECONDS` | 非传输类账号/服务端错误后的冷却秒数，默认 10 |
| `UPSTREAM_RATE_LIMIT_COOLDOWN_SECONDS` | 429 缺少有效 Retry-After 时的冷却秒数，默认 60 |
| `UPSTREAM_CAPACITY_WAIT_SECONDS` | RPM、并发或最小间隔受限时最多等待的秒数，默认 15；设为 0 立即返回 429 |
| `MAX_REQUEST_BYTES` | 普通请求体大小限制，默认 16777216 字节（16 MiB） |
| `MAX_IMAGE_REQUEST_BYTES` | 包含图片输入的请求体大小限制（base64 图片会放大 JSON），默认 67108864 字节（64 MiB） |
| `MODEL_CATALOG` | `/v1/models` 展示列表，默认 `gpt-6.1-sol`；不限制请求模型，留空返回空目录 |
| `DEFAULT_INSTRUCTIONS` | Chat 和旧版 Responses 适配层缺少指令时使用的默认提示；原生 Responses 不补充 |
| `OAUTH_*` | OAuth 地址、客户端标识、回调地址和 PKCE 会话配置 |

模型访问策略由数据库黑名单控制，默认全部放行。项目没有自动发现或选择“最新模型”的逻辑。

多账号池仅在 `oauth_manual` 模式启用，推荐使用此模式。文件和环境变量模式仍使用单个账号。`codex_auth_file` 从 `CODEX_HOME/auth.json` 读取 access token，文件模式不会自行刷新登录态，需要外部维护；`env_token` 需要设置 `UPSTREAM_BEARER_TOKEN`。所有模式都需要可解析的 ChatGPT account ID，不能用任意官方 API Key 代替。

Docker 镜像未安装 Codex CLI；如使用文件模式，需要挂载认证文件。如使用 CLI 登录管理接口，还需自行准备含 Codex CLI 的运行环境。

`.env.docker.example` 用于 Docker 部署，`.env.example` 用于本地开发。Docker 中 `HOST=0.0.0.0`、容器内端口固定为 `8080`，宿主机绑定地址和端口由 `GATEWAY_BIND_ADDRESS`、`GATEWAY_HOST_PORT` 配置，默认只监听 `127.0.0.1:34567`。Compose 会覆盖 `.env` 中的 `PORT`，避免应用监听端口与健康检查、端口映射不一致；数据库与密钥文件路径也相对于容器内的 `/app`。

需要让远程客户端访问时，在服务器 `.env` 设置：

```env
GATEWAY_BIND_ADDRESS=0.0.0.0
GATEWAY_HOST_PORT=34567
```

然后重新创建 Gateway：

```bash
podman-compose up -d --force-recreate
```

同时在云平台安全组和服务器防火墙放行 TCP `34567`。公网暴露前应在反向代理或防火墙层限制来源；API Key 只作为应用鉴权，不会限制端口扫描或连接建立。

修改 `.env` 后执行 `docker compose up -d` 让配置生效，单独执行 `docker compose restart` 不会重新加载容器环境变量。Compose 在 `environment` 中固定设置上游代理，修改 `.env` 中的 `UPSTREAM_PROXY_URL` 不会覆盖它。账号限额和模型黑名单保存在数据库中，通过管理命令或管理员接口修改即可，无需重启。

`podman-compose 1.0.6` 可能无法识别仅 `.env` 内容的变化；修改后执行 `podman-compose up -d --force-recreate`，重新创建容器并加载配置。该操作会短暂停止两个服务，保留宿主机数据目录。

## 更新与备份

### 数据位置

| 宿主机路径 | 内容 |
| --- | --- |
| `.env` | 管理员口令、运行配置及可能配置的加密密钥 |
| `data/gateway.db` | 用户、Key 摘要、账号凭证、限额、模型黑名单与用量 |
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

启动服务和执行数据库管理命令时会自动进行幂等迁移，包括旧凭证加密、软删除、账号池及限额字段、模型黑名单表。迁移按 schema 版本执行，密钥校验失败会回滚。不要把旧代码直接指向已升级数据库作为回滚方案；需要回滚时使用与备份兼容的代码和数据。

### 账号池 SSE 修复的发布与回滚

先在本地实现、测试和审查，再安排发布。核对 `git branch --show-current`、`git rev-parse HEAD`、`git status --short` 及线上两个应用容器的镜像 ID，保留未提交修改；不要在有本地修改时直接拉取覆盖。发布完整的已审查版本，不能只热拷贝 `services.py` 或替换运行中容器的文件。

账号池 SSE 修复引入 `upstream_attempts` 表、`usage_logs.gateway_request_id` 可空列及索引，启动时自动幂等迁移，旧日志与用量保留。项目也已包含用户路由、身份字段和下游限额功能；线上若尚无 `user_upstream_routes`，需连同相应代码与迁移一起核对和测试。不要手工在生产数据库执行 DDL。先在隔离目录用脱敏的数据副本及匹配密钥验证升级；勿把该副本连接到生产服务。

原生 Responses 与逐次日志更新另外增加 `usage_logs.retry_count/is_final/upstream_http_status/failure_category/elapsed_ms` 及 `upstream_attempts.retry_count`，均为增量迁移。历史日志 `is_final` 默认为 true、重试次数保持空，保留原统计口径。Gateway 与 Admin 必须同时更新，避免新版逐次日志被旧后台重复计为用户请求。

重试原因诊断增量增加 `upstream_attempts.retry_reason/preview_stop_reason/response_headers_ms/preview_elapsed_ms/failure_event_ms` 五个可空字段，启动自动幂等迁移。旧尝试链路与耗时保留，新诊断保持空，不推测历史请求未重试的原因。Gateway 和 Admin 使用相同版本；无需手工修改线上数据库。

发布范围需特别核对：此前下游限额功能将所有新旧用户的默认并发设为 2，可能新增 429；从不含该功能的旧版本升级时，需要根据实际负载核对策略。原生转发和逐次日志更新不调整该限额，也未增加账号＋模型健康避让。

发布前保留 Gateway 和 Admin 的旧镜像 ID/固定标签、Compose、`.env`，在维护窗口停止两个应用服务并按上一节备份数据库及对应密钥。使用固定发布版本构建两个应用镜像，记录新镜像 ID；只重建应用服务，不更换 WARP、超时和请求限额。验证 Compose 配置后启动 Gateway，再启动 Admin，避免两个进程同时执行首次迁移。确认 `/healthz`、`/readyz`、后台登录与旧路由正常，再做经授权的模型冒烟请求，核对短请求 ID、从 0 起的重试次数、每次上游请求 ID、各次日志及一次用户计数。健康端点正常本身不能证明模型调用或客户端网络正常。

行为回滚：将 `.env` 中 `SSE_EARLY_RETRY=false`，重建 Gateway 容器使配置生效，可关闭新增预读取切号，保留诊断和新增表；其他此前待提交功能不会因此回滚。整版回滚：停止两个应用，恢复已记录的旧镜像及对应配置，再按已验证的兼容方案启动。新增表/可空列无需在线删除，但旧代码是否兼容整个升级后的 schema 必须先用副本验证；未验证时使用升级前备份和匹配密钥离线恢复，恢复会丢失备份后新产生的数据。禁止在线删表或改库回滚。

原生转发行为可用 `RESPONSES_PASSTHROUGH=false` 切回旧适配层，逐次日志和退避仍生效。若整版回滚到只理解单条用量日志的旧后台，应使用匹配备份或先验证统计兼容性，不能仅因增量列可以保留就假设统计结果也相同。

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

### 图片请求提示 `413 Payload Too Large`

图片通常以内嵌 base64 data URL 放在 JSON 中，体积会比原始图片大约三分之一。网关对包含图片字段的 Chat Completions、Responses 和图像接口默认允许 64 MiB；普通请求仍使用 16 MiB 限制。已有部署如果 `.env` 是从旧模板创建的，请补上：

```env
MAX_IMAGE_REQUEST_BYTES=67108864
```

然后重建并重启 Gateway：

```bash
docker compose up -d --build --force-recreate looper-ai-gateway
```

如果图片仍然超过 64 MiB，可在确认服务器内存足够后提高该值；客户端也可以先压缩图片，或改用可访问的图片 URL，减少请求体积。

### model blocked 与 model_not_found 有什么区别

| 错误 | 来源 | 处理方式 |
| --- | --- | --- |
| `model blocked: gpt-6.1-sol` | 网关数据库黑名单 | 通过 `unblock-model` 或管理员接口移除，无需重启 |
| 上游返回 `model_not_found` 或 `not supported when using Codex with a ChatGPT account` | 上游模型或账号权限 | 使用当前账号有权访问的模型；工作区账号还需管理员开放相应模型 |

如果仍看到旧错误 `model not allowed`，说明容器还在运行白名单版本。更新并重建网关镜像：

```bash
git pull --ff-only
podman-compose build looper-ai-gateway
podman-compose up -d --force-recreate
podman-compose exec looper-ai-gateway python manage.py list-blocked-models
```

首次升级黑名单应为空（`[]`），之后按第 7 步实际调用。网关放行仅允许转发，不授予上游访问权限；上游不支持或账号无权使用的模型仍会返回错误。

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

Gateway 只依赖 WARP 容器先创建，不会在 Compose 层无限等待健康条件。WARP 运行中变为 unhealthy 不会自动停止或重启 Gateway，需结合日志与实际代理请求判断。

如果 `podman-compose up -d` 长时间没有返回，先按 `Ctrl-C`，再直接查看容器健康状态和最近一次检查结果。Podman 的健康检查可以单独执行，退出码 `0` 表示成功，`1` 表示检查失败：

```bash
WARP_CONTAINER=looper-ai-gateway_warp_1
podman ps -a --filter "name=$WARP_CONTAINER"
podman inspect --format '{{.State.Status}} health={{.State.Health.Status}}' "$WARP_CONTAINER"
podman inspect "$WARP_CONTAINER" | jq '.[0].State.Health'
podman healthcheck run "$WARP_CONTAINER"; echo "healthcheck_exit=$?"
podman exec "$WARP_CONTAINER" sh -x /usr/local/bin/warp-healthcheck.sh
podman logs --tail=200 "$WARP_CONTAINER"
```

根据最后一条命令的输出处理：

| 现象 | 处理 |
| --- | --- |
| `Could not resolve host` | 检查服务器 DNS、Podman 网络和出站 DNS |
| `Connection refused` 或 1080 无监听 | 查看 WARP 日志，等待首次注册；必要时提高 `.env` 的 `WARP_SLEEP` 后 `podman-compose up -d --force-recreate` |
| `Operation not permitted`、TUN 错误 | 确认 `/dev/net/tun` 存在，并允许 `NET_ADMIN`、`MKNOD`、`AUDIT_WRITE`；受限 VPS/LXC 需要服务商开放 TUN |
| trace 能访问但 `warp=off` | WARP 没有建立隧道，检查日志和服务器出站 TCP/UDP 443 |
| `warp=on` 或 `warp=plus` | WARP 健康，继续检查 Gateway 日志和 `/readyz` |

若这是全新服务器且 `warp-data/` 中没有需要保留的注册状态，可以重置 WARP 数据后重建；已有服务器不要删除该目录：

```bash
podman-compose down
mv warp-data "warp-data.backup.$(date +%Y%m%d-%H%M%S)"
mkdir -p warp-data
podman-compose up -d --force-recreate
```

Podman 官方文档也建议使用 `podman inspect` 查看 `.State.Health`，并用 `podman healthcheck run` 手动触发检查。[健康检查文档](https://docs.podman.io/en/stable/markdown/podman-healthcheck.1.html)

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

账号池故障回归集中在 `tests/test_sse_failover.py`：覆盖早期失败切号、缓冲顺序和分片、三种预读取上限、文本/拒绝/工具后的禁止重放、统一尝试预算、绑定与路由、原始错误保留、关闭租约、取消与发送时断开、内部异常分类和单次最终用量。账号＋模型健康避让尚未实现，对应的到期探测及多 worker 一致性验证不在本版覆盖范围。
