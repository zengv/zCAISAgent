# 06 · 部署手册

> 适用版本：M3（2026-11）｜ 覆盖本地开发、Docker Compose、单机生产三种运行形态。
> 本文所有命令均在仓库根目录执行；Windows 使用 PowerShell。

---

## 1. 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **3.12+** | `pyproject.toml` 要求 `>=3.12`，使用 `tomllib` 读取配置 |
| Node.js | **不需要** | Chat 与 Studio 都是 FastAPI 同源托管的原生 HTML/CSS/JS，无前端构建链 |
| Docker | 可选 | 仅在需要容器化运行时安装 Docker Engine + Compose v2 |
| 操作系统 | Linux / Windows | 单机生产推荐 Linux；Windows 可用服务或计划任务托管 |

依赖分层：

- 必装：`pydantic`、`fastapi`、`uvicorn`、`httpx`、`pypdf`、`psycopg[binary]`、`pymysql`
- 开发：`pytest`、`ruff`、`mypy`、`import-linter`（`.[dev]`）
- Redis 缓存（可选）：`redis`（`.[redis]`）
- Postgres / pgvector：通过 DSN 连接，不需要额外 Python 依赖

---

## 2. 本地运行

### 2.1 创建虚拟环境并安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

> PowerShell 禁止执行脚本时，可跳过激活，直接用 `.\.venv\Scripts\python.exe -m pip install ...`。
> 只部署运行时用 `python -m pip install -e .`；需要 Redis 缓存再加 `.[redis]`。

### 2.2 环境变量与配置文件

```powershell
Copy-Item .env.example .env
```

- `.env`：只放扁平运行参数与密钥；普通运行参数进程环境变量优先于 `.env`。
- `config.toml`：模型 provider、embedding、rerank、检索、数据库、安全、工具、记忆、缓存、调度、事件、Studio 路径、智能问数（`[n2sql]`）。
- 外部模型密钥通过 `config.toml` 中 `key_env` 声明的变量名引用，例如 `AI_KEY_DS`、`AI_KEY_MS`、`AI_KEY_QW`；这些密钥**优先从 `.env` 读取**，缺失时回退系统环境变量。**不要把密钥写进 `config.toml` 或提交到仓库**（`.env` 已在 `.gitignore` 中）。
- 自定义路径：`ZCAISAGENT_CONFIG_FILE`、`ZCAISAGENT_ENV_FILE`。

常用运行参数（均有默认值）：

```powershell
$env:ZCAISAGENT_API_KEY = "dev-key"     # 生产必须修改
$env:ZCAISAGENT_DEFAULT_TENANT = "demo"
$env:ZCAISAGENT_DATA_DIR = ".zcaisagent"
$env:ZCAISAGENT_SEED_DIR = "data/seed"
```

### 2.3 FakeLLM 零密钥启动

容器只有在 **没有任何 `[model_providers.*]`** 时才装配 `FakeLLM`。仓库自带的 `config.toml` 已定义 provider，
因此零密钥演示需要临时指向一个不含 provider 的配置：

```powershell
$env:PYTHONPATH = "src"
$env:ZCAISAGENT_CONFIG_FILE = "$env:TEMP\zcaisagent-offline.toml"
New-Item -ItemType File -Path $env:ZCAISAGENT_CONFIG_FILE -Force | Out-Null
uvicorn zcaisagent.api.app:app --host 127.0.0.1 --port 8080
```

空 TOML 会回退到内置默认值：FakeLLM、内置 `admin` 角色（含 `admin:read` / `admin:write`）、
`dev-key` API Key、`data/seed` 种子语料。FakeLLM 只用于联调与演示，回答质量不代表真实模型。

> 如果 `config.toml` 里有 provider 但对应 `AI_KEY_*` 为空，系统**不会**自动切换到 FakeLLM；
> 请求会按 provider 顺序失败并把 Run 收尾为 `failed`。离线演示请使用上面的空配置方式。

### 2.4 启动与验证

```powershell
$env:PYTHONPATH = "src"
uvicorn zcaisagent.api.app:app --host 127.0.0.1 --port 8080
```

| 入口 | 地址 | 用途 |
|---|---|---|
| Chat | <http://127.0.0.1:8080/> | 对话与引用查看，首次在左侧填 `X-API-Key` |
| Studio | <http://127.0.0.1:8080/studio> | 数据源 / 轨迹 / 评估 / 配置四页 |
| 健康检查 | <http://127.0.0.1:8080/health> | 组件健康快照 |

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

### 2.5 Windows 一键控制台

双击或执行项目根目录的 `~start-backend.bat`：

```powershell
.\~start-backend.bat
# 1 = 启动   2 = 重启   0 = 停止   Q = 退出
```

脚本会依次探测 `.venv` → PATH `python` → conda 环境 → `python3`，以
`uvicorn zcaisagent.api.app:app --host 127.0.0.1 --port 8080` 后台启动，日志写入
`.zcaisagent/backend.log`（stderr）与 `.zcaisagent/backend.log.out`（stdout）。

### 2.6 智能问数（可选 MySQL）

智能问数默认随 `[n2sql] enabled` 生效；需要一份**只读 MySQL 账号**，连接参数只从环境变量读取。

```powershell
# .env 或系统环境变量
$env:ZCAISAGENT_N2SQL_HOST = "127.0.0.1"
$env:ZCAISAGENT_N2SQL_PORT = "3306"
$env:ZCAISAGENT_N2SQL_USER = "zcais_ro"
$env:ZCAISAGENT_N2SQL_PASSWORD = "******"
$env:ZCAISAGENT_N2SQL_DATABASE = "school"
```

`config.toml` 需确认 `[n2sql]` 的 `enabled` / `tables` / `schema`，以及 `[[n2sql.users]]` 的用户范围（见仓库默认配置）。

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8080/v1/n2sql `
  -Headers @{"X-API-Key"="dev-key"} `
  -Body (@{question="S1 的成绩是多少？"; user_id="s_s1"} | ConvertTo-Json)
```

| 返回 | 含义 |
|---|---|
| 503 | 未配置 `[n2sql]`、`enabled=false` 或连接参数缺失 |
| 403 | `user_id` 未在 `[[n2sql.users]]` 中，或缺少 `tool:read` |
| 400 | 问题为空，或生成的 SQL 未通过只读/白名单校验 |
| 502 | 模型生成失败，或 MySQL 连接/执行失败 |

> MySQL 账号必须只读，只授予 `tables` 白名单表的 `SELECT`；不要使用 `root`。数据库不可用时只有 `n2sql` 组件降级，不影响检索与其他接口。

---

## 3. Docker Compose

### 3.1 默认启动（仅 API，FakeLLM）

```powershell
docker compose up --build
```

- 镜像基于 `python:3.12-slim`，只复制 `src`、`data` 与构建所需的 `pyproject.toml` / `README.md`，**不复制 `config.toml`**，因此容器内没有 provider，自动使用 `FakeLLM`。
- 端口：`8080:8080`；数据卷：`zcaisagent_data:/app/.zcaisagent`。
- 访问 `/`、`/studio`、`/health` 与本地一致。
- `api` 已配置 `healthcheck`，每 30 秒请求一次 `http://127.0.0.1:8080/health`。

> `/health` 在组件降级时仍返回 HTTP 200，只在响应体把 `status` 标为 `degraded`。
> 因此容器健康检查只验证“进程与 HTTP 可达”，组件级状态要看响应体或 Studio。

### 3.2 追加 Postgres / Redis（可选）

`postgres` 与 `redis` 放在 `infra` profile 中，默认不启动：

```powershell
docker compose --profile infra up --build
```

**Postgres**：数据库开关由环境变量驱动，镜像没有 `config.toml` 也能启用：

```powershell
$env:ZCAISAGENT_DATABASE_ENABLED = "true"
docker compose --profile infra up --build
```

`api` 通过 `ZCAISAGENT_DATABASE_URL` 连接 compose 内的 `postgres` 服务；连接失败会回退内存检索，
健康板 `database=down`。DSN 中的账号密码只是本地开发默认值，生产必须通过环境变量覆盖或改用外部数据库。

**Redis**：缓存开关要求配置中存在 `[cache]` 段，而默认镜像不包含 `config.toml`，所以默认不启用缓存。
需要 Redis 时：

1. 准备一份容器配置，至少包含：

   ```toml
   [cache]
   enabled = true
   backend = "redis"
   redis_url = "redis://redis:6379/0"
   ```

2. 把该文件挂载到 `/app/config.toml`，并把 `docker-compose.yml` 中
   `ZCAISAGENT_CACHE_BACKEND` 从 `memory` 改为 `redis`（该环境变量会覆盖配置文件里的 `backend`）；
3. 用 `docker compose --profile infra up --build` 启动。

Redis 连接失败会回退进程内缓存，健康板 `cache=down`。若挂载的是仓库默认 `config.toml`，
容器也会读到其中的 `[model_providers.*]`，需要同时注入对应 `AI_KEY_*`，否则模型调用会失败。

- 生产不要暴露 `5432` / `6379` 到公网；在 compose 或网关层删除对应 `ports`。

### 3.3 本机没有 Docker 时

用第 2 节的方式直接运行 uvicorn 即可；也可用 Python 检查 Compose 文件结构：

```powershell
python -c "import yaml, pathlib; data=yaml.safe_load(pathlib.Path('docker-compose.yml').read_text(encoding='utf-8')); print(list(data['services']))"
```

（`pyyaml` 不是运行依赖，仅用于本地结构检查。）

---

## 4. 单机生产

### 4.1 进程管理（systemd）

```ini
# /etc/systemd/system/zcaisagent.service
[Unit]
Description=zCAISAgent API
After=network-online.target

[Service]
Type=simple
User=zcais
Group=zcais
WorkingDirectory=/opt/zcaisagent
Environment=PYTHONPATH=/opt/zcaisagent/src
EnvironmentFile=/etc/zcaisagent.env
ExecStart=/opt/zcaisagent/.venv/bin/uvicorn zcaisagent.api.app:app --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zcaisagent
sudo systemctl status zcaisagent
journalctl -u zcaisagent -f
```

> 当前版本请保持 **单 worker**（uvicorn 默认）：数据源启停、记忆、进程内缓存、调度器都是进程内状态，
> 多 worker 会各自持有一份，且 JSONL checkpoint / trace / audit 并发写入需要额外设计。

### 4.2 Windows 服务

推荐用 [NSSM](https://nssm.cc/) 或等价的服务包装器：

```powershell
nssm install zCAISAgent "C:\zcaisagent\.venv\Scripts\python.exe" "-m uvicorn zcaisagent.api.app:app --host 127.0.0.1 --port 8080"
nssm set zCAISAgent AppDirectory "C:\zcaisagent"
nssm set zCAISAgent AppEnvironmentExtra "PYTHONPATH=C:\zcaisagent\src" "ZCAISAGENT_CONFIG_FILE=C:\zcaisagent\config.toml"
nssm set zCAISAgent AppStdout "C:\zcaisagent\.zcaisagent\backend.log.out"
nssm set zCAISAgent AppStderr "C:\zcaisagent\.zcaisagent\backend.log"
nssm start zCAISAgent
```

生产 Windows 也可改用计划任务（开机启动、失败重启）；无论哪种方式，服务账号只需
对 `.zcaisagent/`、`data/` 与配置文件有读写权限。

### 4.3 反向代理与 TLS

uvicorn 只监听 `127.0.0.1:8080`，由 Nginx/Caddy 终止 TLS 并转发：

```nginx
server {
    listen 443 ssl http2;
    server_name agent.example.com;

    ssl_certificate     /etc/letsencrypt/live/agent.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agent.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE：关闭缓冲并放宽读超时，否则 /v1/chat 会整段缓冲或提前断开
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

- 只暴露 443；8080、5432、6379 一律留在内网。
- 如需跨域浏览器访问，在网关层配置白名单；应用本身没有注册 CORS 中间件，默认只服务同源页面。
- 证书可用 certbot / 企业证书；HTTP 80 建议只做 301 跳转。

### 4.4 环境变量清单

密钥只从环境变量（或 `.env`）注入，不写入镜像与配置文件。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `ZCAISAGENT_API_KEY` | `dev-key` | API Key，生产必须修改 |
| `ZCAISAGENT_DEFAULT_TENANT` | `demo` | 默认租户 |
| `ZCAISAGENT_DATA_DIR` | `.zcaisagent` | checkpoint / trace / audit / 死信目录 |
| `ZCAISAGENT_SEED_DIR` | `data/seed` | 种子语料目录 |
| `ZCAISAGENT_TOP_K` | `5` | 检索返回条数 |
| `ZCAISAGENT_MAX_EVIDENCE` | `8` | 送入上下文的证据上限 |
| `ZCAISAGENT_NODE_TIMEOUT_S` | `30` | 单节点超时 |
| `ZCAISAGENT_INPUT_PRICE_PER_MTOK` / `OUTPUT_PRICE_PER_MTOK` | `0` | 成本估算单价 |
| `ZCAISAGENT_CONFIG_FILE` / `ZCAISAGENT_ENV_FILE` | 自动探测 | 配置与 `.env` 路径 |
| `AI_KEY_DS` / `AI_KEY_MS` / `AI_KEY_QW` | 空 | 对应 provider 的模型密钥（`key_env` 声明，优先读 `.env`） |
| `ZCAISAGENT_EMBEDDING_ENABLED` | 跟随配置 | 覆盖 `[embedding] enabled` |
| `ZCAISAGENT_RERANK_ENABLED` | 跟随配置 | 覆盖 `[rerank] enabled` |
| `ZCAISAGENT_HYDE_ENABLED` / `ZCAISAGENT_PARENT_CHILD_ENABLED` | 跟随配置 | 检索增强开关 |
| `ZCAISAGENT_DATABASE_ENABLED` | 跟随配置 | 覆盖 `[database] enabled` |
| `ZCAISAGENT_DATABASE_URL` | 空 | pgvector DSN（`dsn_env` 默认值） |
| `ZCAISAGENT_SECURITY_IDENTITY` | 跟随配置 | `api_key` / `oidc` |
| `ZCAISAGENT_OIDC_CLIENT_SECRET` | 空 | OIDC introspection 客户端密钥 |
| `ZCAISAGENT_TOOLS_ENABLE_KB_SEARCH` / `TOOLS_ENABLE_TICKET` | 跟随配置 | 内置工具开关 |
| `ZCAISAGENT_MEMORY_ENABLED` / `MEMORY_DISTILL_ENABLED` / `MEMORY_MAX_ITEMS` / `MEMORY_TTL_S` | 跟随配置 | 记忆开关与容量 |
| `ZCAISAGENT_CACHE_ENABLED` / `CACHE_TTL_S` / `CACHE_MAX_ENTRIES` / `CACHE_LLM_ENABLED` | 跟随配置 | 缓存参数 |
| `ZCAISAGENT_CACHE_BACKEND` | `memory` | `memory` / `redis` |
| `ZCAISAGENT_REDIS_URL` / `REDIS_TIMEOUT_S` | 配置值 | Redis 连接与超时 |
| `ZCAISAGENT_SCHEDULER_ENABLED` / `SCHEDULER_TICK_S` | 跟随配置 | 调度器开关与轮询间隔 |
| `ZCAISAGENT_EVENTS_ENABLED` / `EVENTS_WEBHOOK_URLS` / `EVENTS_DEAD_LETTER` | 跟随配置 | Webhook 投递 |
| `ZCAISAGENT_EVENT_SECRET` | 空 | Webhook 签名密钥（`secret_env` 默认值） |
| `ZCAISAGENT_SECRET_PROVIDER` / `VAULT_ADDR` / `VAULT_TOKEN` | `env` / 空 | 密钥来源 |
| `ZCAISAGENT_STUDIO_EVAL_REPORT` / `STUDIO_EVAL_GATE` / `STUDIO_BASELINE_DIR` | 配置值 | Studio 评估页路径 |
| `ZCAISAGENT_N2SQL_ENABLED` | 跟随 `[n2sql] enabled` | 智能问数开关 |
| `ZCAISAGENT_N2SQL_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_DATABASE` | 空 / `3306` | 只读 MySQL 连接；由 `*_env` 指向，密码只放环境变量 |
| `ZCAISAGENT_N2SQL_MAX_ROWS` / `_TIMEOUT_S` | 配置值 | 查询行数上限与超时 |

### 4.5 数据目录与权限

| 路径 | 内容 | 备份 |
|---|---|---|
| `.zcaisagent/checkpoints/` | 每个 Run 的 JSONL 状态快照（追加写） | 必须 |
| `.zcaisagent/trace.jsonl` | 节点级轨迹（追加写） | 必须 |
| `.zcaisagent/audit.jsonl` | 审计事件（追加写） | 必须 |
| `.zcaisagent/dead_letter.jsonl` | Webhook 最终失败记录 | 建议 |
| `data/seed/` | 种子语料 | 必须 |
| `config.toml`、`configs/` | 非密钥配置与门禁阈值 | 必须 |
| `data/eval/latest.json`、`data/eval/baselines/` | 评估报告与基线 | 建议 |
| `data/perf/`、`data/drills/` | 压测与演练报告 | 建议 |

Linux：

```bash
sudo useradd -r -s /sbin/nologin zcais
sudo chown -R zcais:zcais /opt/zcaisagent/.zcaisagent /opt/zcaisagent/data
sudo chmod 750 /opt/zcaisagent/.zcaisagent /opt/zcaisagent/data
sudo chmod 600 /etc/zcaisagent.env
```

Windows：用专用服务账号，`icacls` 只授予该账号对 `.zcaisagent` 与 `data` 的修改权限；
`.env` 不对普通用户开放。

---

## 5. 可选依赖与回退

| 依赖 | 开启方式 | 缺失 / 失败时的行为 |
|---|---|---|
| LLM provider | `[model_providers.*]` + `AI_KEY_*` | 没有 provider → `FakeLLM`；有 provider 但密钥错误 → 按 fallback 链尝试，全部失败 Run 以 `failed` 收尾（`NODE_FAILED`） |
| Embedding | `[embedding] enabled=true` | `vector` 源与语义重排不可用，词法 / BM25 检索继续工作，轨迹记 `degraded` |
| 专用 rerank | `[rerank] enabled=true` + 端点 | 回退顺序：专用端点 → embedding 语义余弦 → LLM 打分 → 词法相关性 |
| Postgres / pgvector | `[database] enabled=true` + DSN | 启动回退内存检索 + 种子语料，健康板 `database=down`；需重启后才会重连 |
| Redis | `[cache] backend="redis"` + `.[redis]` | 回退进程内缓存；健康板 `cache=down`，`/health` 汇总为 `degraded` |
| Vault | `[secret] provider="vault"` + `VAULT_ADDR` / token | token 缺失或读取失败回退环境变量，健康板 `secret=down` |
| Webhook | `[events] enabled=true` + URL | 关闭时不投递；5xx/429/网络异常重试 `max_retries` 次后写 `dead_letter_path` |
| MCP | `[tools.mcp.*]` 或 `mcp_servers` | 子进程启动/发现失败只标记 `tools.mcp.<name>=down`，不阻断启动 |
| OpenAPI | `[tools.openapi.*]` | spec 加载失败标记 `tools.openapi.<name>=down`，其他工具可用 |
| 只读 SQL | `[tools.sql.*]` | 路径/表白名单非法标记 `tools.sql.<name>=down` |
| 智能问数 MySQL | `[n2sql] enabled=true` + `pymysql` + 连接环境变量 | 驱动缺失或连接参数不全 → `n2sql` 健康项 `down`，`/v1/n2sql` 返回 503；其他能力不受影响 |
| OIDC | `[security] identity="oidc"` | 配置不完整回退 API Key，`security.identity=down` |
| OPA | `[security.opa] url` | 端点异常时策略拒绝（fail closed） |

---

## 6. 安全清单

- [ ] 修改默认 `ZCAISAGENT_API_KEY`，禁止把 `.env` 提交到仓库。
- [ ] `admin:read` / `admin:write` 只授予运维角色；普通业务角色不要带 admin scope。
- [ ] uvicorn 绑定 `127.0.0.1`，只通过反向代理暴露；不暴露 PG / Redis 端口。
- [ ] 应用未启用 CORS；若需跨域，在网关配置显式白名单，不要放开 `*`。
- [ ] 密钥只从环境变量 / Vault 读取；日志、镜像、Compose 文件不写入真实密钥。
- [ ] 保护 `.zcaisagent/`：审计与轨迹含用户问题、答案片段、工具参数摘要，按敏感数据管理。
- [ ] 日志脱敏：服务不打印 API Key；Redis 错误会遮蔽连接 URL，Webhook 死信会遮蔽签名密钥；采集侧仍应禁止记录 `X-API-Key`、`.env` 与完整 prompt。
- [ ] 审计 `audit.jsonl` 只追加：不要原地编辑；需要更强保证时用 OS append-only 属性或只读挂载消费。
- [ ] Studio 的 API Key 只保存在浏览器 `sessionStorage`，关闭标签页即失效；不要把 Key 放进 URL。
- [ ] 定期轮换模型密钥、OIDC 客户端密钥与 Webhook 签名密钥。
- [ ] 智能问数使用独立只读 MySQL 账号，只授予白名单表 `SELECT`，禁止 `root` 与写权限。
- [ ] `[[n2sql.users]]` 只配置确需访问的用户；未知用户默认拒绝；MySQL 端口不暴露公网。
- [ ] 生产删除或收紧 `dev` / `web` 等默认 admin 映射；`/v1/n2sql` 的 `user_id` 由可信网关注入，不要直接信任客户端任意传值。

---

## 7. 升级与回滚

### 7.1 升级

1. 备份：见第 4.5 节，至少覆盖 `.zcaisagent/`、`data/seed/`、`config.toml`、`configs/`；启用 PG 时执行 `pg_dump -Fc`。
2. 停止接收新请求（网关摘流），等待进行中的 SSE 连接结束，再停进程。
3. 更新代码与依赖：`git pull` 后 `python -m pip install -e .`；容器方式重新 `docker compose build`。
4. 启动并验证：`/health`、`/studio`、一次 `POST /v1/runs`、`GET /v1/runs/{id}/trace`。
5. 可选回归：`python -m zcaisagent.cli eval-gate --path data/eval/golden.jsonl`
   （FakeLLM 下通常达不到门禁阈值，返回 1 属预期；真实模型环境再作为发布门槛）。

### 7.2 回滚

1. 停止进程，恢复上一版本代码 / 虚拟环境 / 镜像 tag。
2. 如果新版本写入过数据且格式不兼容，用升级前备份恢复 `.zcaisagent/` 与 PG。
3. 恢复 `config.toml` 与 `configs/`，启动后再次检查 `/health` 与 Studio 四页。
4. JSONL 为追加写，回滚时不要把新旧版本写入混在同一份 checkpoint / audit 中；必要时整体替换目录。

> 当前版本没有数据库 schema 迁移脚本；升级前备份、回滚时整体恢复是最稳妥方式。
