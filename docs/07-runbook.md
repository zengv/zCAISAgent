# 07 · 运维手册（Runbook）

> 适用版本：M3（2026-11）｜ 面向值班与交付同学，配合 [06 · 部署手册](06-deployment.md) 使用。

---

## 1. 健康检查

### 1.1 `/health` 响应

```json
{
  "status": "ok",
  "service": "zcaisagent",
  "entries": [
    {"name": "runtime", "owner": "runtime", "status": "ok", "message": "", "updated_at": 0.0}
  ]
}
```

- `status`：全部条目为 `ok` 时是 `ok`，否则为 `degraded`；**HTTP 始终返回 200**，不要只看状态码。
- 条目 `status` 取值：`unknown` / `ok` / `degraded` / `down`。
- `message` 用于展示最近一次错误或模式（如 `env`、`vault`、`disabled`）。

### 1.2 组件项含义

| 条目 | 何时出现 | 含义与排查 |
|---|---|---|
| `runtime` | 始终 | 编排运行时；节点抛异常时会被置为 `down`，同时 Run 以 `failed`（`NODE_FAILED`）收尾 |
| `llm` | 始终 | 模型端口装配状态；默认 `ok`，模型调用失败主要体现在 Run 终态与 trace |
| `retrieval` | 始终 | 检索引擎装配状态；单源失败不置 `down`，看 trace 的 `degraded` 备注 |
| `checkpoint` | 始终 | JSONL checkpoint 端口状态 |
| `trace` | 始终 | JSONL 轨迹端口状态 |
| `cache` | 始终 | `message` 显示 `disabled` / `memory` / `redis`；Redis 回退时由 `/health` 覆盖为 `down` |
| `database` | 启用 `[database]` 时 | `ok` 表示 Postgres 已连接并载入 chunk；失败为 `down` 并回退内存 |
| `secret` | 始终 | `env` / `vault`；Vault token 缺失或读取失败为 `down`（已回退 env） |
| `events` | 始终 | 关闭时为 `ok` + `disabled`；启用后显示 webhook 数量 |
| `security.identity` | 始终 | `api_key` / `oidc`；OIDC 配置不完整为 `down`（已回退 API Key） |
| `security.policy` | 始终 | `rbac` / `opa` |
| `tools.mcp.<name>` | 配置了 MCP 源 | 子进程启动/工具发现失败为 `down`，不影响其他工具 |
| `tools.openapi.<name>` | 配置了 OpenAPI 源 | spec 加载失败为 `down` |
| `tools.sql.<name>` | 配置了 SQL 源 | 路径/白名单非法为 `down` |
| `n2sql` | 配置了 `[n2sql]` 时 | `disabled` / 连接环境变量缺失 / `mysql host:port/db`；失败只影响智能问数，`/v1/n2sql` 返回 503 |

> 检索源没有独立健康项；数据源页返回 `SourceRegistry` 的状态，并把源名映射到 `/health` 同名条目。
> 由于当前 `/health` 不注册 `inmemory` / `bm25` / `vector` 等条目，数据源页的 `health` 通常显示 `unknown`，
> 真实降级证据以 trace notes 与 Run 结果为准。

---

## 2. 观测入口

| 入口 | 路径 | 用途 |
|---|---|---|
| Chat | `/` | 人工复现问题、查看答案与引用 |
| Studio | `/studio` | 四页：数据源、轨迹、评估、配置；顶部指标条 |
| 指标 API | `GET /v1/admin/metrics?window=24h` | 请求数、成功率、P50/P95/P99、tokens、成本、缓存命中、质量门禁 |
| 管理员轨迹 | `GET /v1/admin/runs/{run_id}/trace` | 同租户只读，返回 `run` 摘要 + `events` |
| 用户轨迹 | `GET /v1/runs/{run_id}/trace` | 仅 Run 归属用户可读 |
| 审计 | `.zcaisagent/audit.jsonl` | 追加写 JSONL：`action` / `actor` / `tenant_id` / `run_id` / `detail`（无独立时间戳） |
| 智能问数审计 | `.zcaisagent/audit.jsonl` | `action=n2sql.query`，`detail` 含 `outcome` / `scope` / `sql_digest` / `row_count` |
| 智能问数轨迹 | `.zcaisagent/trace.jsonl` | 节点 `n2sql`；`llm.called` 记录 `n2sql_generate` / `n2sql_answer` 的 prompt、原始响应与耗时 |
| 轨迹 | `.zcaisagent/trace.jsonl` | 每步 `step.finished`：节点、耗时、tokens、成本、输入摘要、输出摘要 |
| Checkpoint | `.zcaisagent/checkpoints/<run_id>.jsonl` | 每个 Run 的状态历史，最后一行为最新状态 |
| 死信 | `.zcaisagent/dead_letter.jsonl` | Webhook 最终失败记录（事件、URL、尝试次数、状态、错误） |
| 进程日志 | `.zcaisagent/backend.log*`（bat 启动） | uvicorn stdout/stderr |

`/v1/admin/*` 需要 API Key + `admin:read`；写操作需要 `admin:write`。默认 `dev-key` 在未配置
`[security]` 角色表或使用仓库默认配置时都带 admin scope；生产应收窄角色权限。

### 2.1 常用排查命令

```powershell
# 健康
Invoke-RestMethod http://127.0.0.1:6278/health

# 指标（最近 24 小时）
Invoke-RestMethod "http://127.0.0.1:6278/v1/admin/metrics?window=24h" -Headers @{"X-API-Key"="dev-key"}

# 最近 50 条 Run
Invoke-RestMethod "http://127.0.0.1:6278/v1/admin/runs?limit=50" -Headers @{"X-API-Key"="dev-key"}

# 单条轨迹
Invoke-RestMethod "http://127.0.0.1:6278/v1/admin/runs/<run_id>/trace" -Headers @{"X-API-Key"="dev-key"}

# 审计中的拒绝事件
Select-String -Path .zcaisagent/audit.jsonl -Pattern "admin.denied|denied"

# 检索降级备注
Select-String -Path .zcaisagent/trace.jsonl -Pattern "retrieve_error|retrieve_degraded"

# 智能问数：按用户问一句（user_id 必须在 [[n2sql.users]] 中）
Invoke-RestMethod -Method Post http://127.0.0.1:6278/v1/n2sql `
  -Headers @{"X-API-Key"="dev-key"} `
  -Body (@{question="S1 的成绩是多少？"; user_id="s_s1"} | ConvertTo-Json)

# 智能问数审计（成功/拒绝/SQL 非法/数据库失败）
Select-String -Path .zcaisagent/audit.jsonl -Pattern "n2sql.query"
```

---

## 3. 常见故障处置

| 现象 | 可能原因 | 处置 |
|---|---|---|
| **401** `{"code":"UNAUTHORIZED"}` | `X-API-Key` 缺失或与 `ZCAISAGENT_API_KEY` 不一致 | 核对请求头与服务端环境变量；审计里看 `admin.denied` 的 `reason=unauthorized` |
| **403** `insufficient scope` | 角色缺少 `admin:read` / `admin:write` | 检查 `[security] roles` 与调用方角色；只给运维角色 admin scope |
| **403** `run not accessible` / `thread not accessible` | 跨租户或跨用户访问 | 用归属租户/用户查询；审计 `run.cross_tenant_denied` / `thread.cross_tenant_denied` |
| 智能问数 **503** | 未配置 `[n2sql]`、`enabled=false` 或连接环境变量缺失 | 健康板看 `n2sql` 条目；按 06 §2.6 补齐 `ZCAISAGENT_N2SQL_*` 并重启进程 |
| 智能问数 **403** | `user_id` 未在 `[[n2sql.users]]`，或缺少 `tool:read` | 补用户目录与角色 scopes；审计 `n2sql.query` 的 `outcome=forbidden` |
| 智能问数 **400** / `N2SQL_INVALID_SQL` | 生成 SQL 非只读、越白名单、含注释/危险对象 | 改问题或补 `tables`/`schema`；查看 trace 的 `n2sql_generate` 原始响应 |
| 智能问数 **502** | 模型生成失败或 MySQL 不可达 | 检查模型 provider 与 MySQL 连通性；审计 `outcome=llm_failed/db_failed` |
| 智能问数结果为空 | 数据范围过滤后无匹配 | 确认 `user_id → role` 的 `student_id`/`teacher_name` 是否正确；不要放宽过滤来"修"空结果 |
| 模型 **429 / 5xx / 超时** | 供应商限流或故障 | 系统会按 fallback 链尝试同等级其他 provider，再向邻近 `effort_levels` 扩展；全部失败时 Run `failed`（`NODE_FAILED`）。检查 provider 密钥、`node_timeout_s`，必要时降并发 |
| 检索 **degraded** | 单源超时/异常/embedding 不可用 | trace `output.notes` 出现 `retrieve_error:<source>` / `retrieve_degraded:<source>`；其余源继续返回。Studio 数据源页可临时停用问题源，或检查 embedding 配置 |
| PG **down** | DSN 错误、网络或 PG 未启动 | 健康板 `database=down`，系统已回退内存 + 种子语料。修复 DSN / 启动 PG 后**重启进程**（连接在启动时建立） |
| Redis **down** | 未装 `redis`、URL/网络错误 | 健康板 `cache=down`，已回退进程内缓存，Run 不受影响。修复后重启进程恢复 Redis 后端 |
| **评估门禁失败** | 指标低于 `configs/eval_gate.toml` 阈值 | 运行 `python -m zcaisagent.cli eval-gate --path data/eval/golden.jsonl` 查看 `violations`；检查最近变更与检索/模型配置 |
| **Webhook 死信** | 对端持续 5xx/429、签名错误或网络不可达 | 查看 `.zcaisagent/dead_letter.jsonl`；修复对端或密钥后手工重放事件；确认 `[events] max_retries` 与签名密钥 |
| **Scheduler skipped** | `concurrency="forbid"` 且同 job 仍有活跃 Run | 审计 `job.skipped`，`detail.reason=concurrency_forbidden`；等待上一条结束或调整 `concurrency` / `misfire` |
| **审批卡住** | Run 停在 `awaiting_user` | 见下方 3.1 |

### 3.1 审批卡住

1. 查 Run：`GET /v1/runs/{run_id}`，确认 `status=awaiting_user` 且 `pending_approval` 非空。
2. `pending_approval.status=pending`：
   - 同意：`POST /v1/runs/{run_id}/approve`，body 可带 `{"reason":"...","actor":"..."}`；
   - 拒绝：`POST /v1/runs/{run_id}/reject`；
   - 两者都会立即恢复执行并返回更新后的 Run。
3. `pending_approval.status` 已是 `approved` / `rejected` 但 Run 仍停住：调用
   `POST /v1/runs/{run_id}/resume` 继续执行。
4. 确认无人处理且不应继续：`POST /v1/runs/{run_id}/cancel`（仅 `awaiting_user` / `running` 可取消）。
5. 审计中核对 `approval.approved` / `approval.rejected` 与 `run.cancelled`。

---

## 4. 备份与恢复

### 4.1 备份内容

| 数据 | 位置 | 说明 |
|---|---|---|
| Run 状态 | `.zcaisagent/checkpoints/` | 每个 Run 一个 JSONL，追加历史 |
| 轨迹 | `.zcaisagent/trace.jsonl` | 节点级执行证据 |
| 审计 | `.zcaisagent/audit.jsonl` | 合规证据，只追加 |
| 死信 | `.zcaisagent/dead_letter.jsonl` | Webhook 失败记录 |
| 语料 | `data/seed/` | 重建内存索引所需 |
| 配置 | `config.toml`、`configs/` | 不含密钥；密钥在环境变量 |
| 评估 | `data/eval/latest.json`、`data/eval/baselines/` | Studio 质量看板 |
| 数据库 | Postgres | 启用 pgvector 时 `pg_dump -Fc -d <db> -f backup.dump` |
| 智能问数业务库 | 外部 MySQL | 由 DBA 按业务库策略备份；本服务只读，不负责写入与恢复 |

建议每日增量备份 `.zcaisagent/` 与 `data/`，每周全量；备份文件与运行目录分离存放，并限制访问权限。

### 4.2 恢复步骤

1. 停止服务，确认没有进行中的 Run。
2. 恢复 `.zcaisagent/`、`data/seed/`、`config.toml`、`configs/`、评估报告到原路径。
3. 启用 PG 时恢复数据库：`pg_restore -d <db> backup.dump`。
4. 校对权限（服务账号可读写数据目录，配置文件与 `.env` 仅管理员可读）。
5. 启动服务，检查 `/health`、`/studio`，按 run_id 抽查 `GET /v1/runs/{id}` 与 trace。
6. 确认审计继续追加，不要覆盖或截断恢复后的 `audit.jsonl`。

---

## 5. 容量与扩容

### 5.1 当前口径

- 单进程部署；uvicorn 保持默认单 worker。
- 数据源启停、记忆、进程内缓存、调度器均为进程内状态。
- 压测报告 `data/perf/loadtest-20-20261111T053016Z.json`（本地 FakeLLM 测试服务，20 并发，40 请求）：
  - 请求数 40，错误 0，RPS 52.41；
  - P50 **192.62 ms**，P95 **382.10 ms**，P99 **426.52 ms**，最大 440.26 ms；
  - 数据来源为一次性本地 FakeLLM 服务（报告中的 `base_url` 为临时端口），**不代表真实云模型**。
- `configs/slo.toml` 阈值（FakeLLM / 本地模型口径）：
  - 默认与 20 并发：P95 ≤ **3000 ms**，错误率 ≤ **1%**；
  - 50 并发：P95 ≤ **6000 ms**，错误率 ≤ **2%**。
  - 真实云模型必须在目标环境复测后更新阈值。

### 5.2 扩容注意事项

| 项 | 单进程 | 多副本 |
|---|---|---|
| Checkpoint / trace / audit | 本地 `.zcaisagent/` | 必须共享存储（NFS/对象存储同步），或改为集中式存储；避免多副本同时写同一 Run |
| 进程内缓存 | `memory` | 切 `redis` 共享；否则各副本命中率独立，指标不可加总 |
| 记忆 | 进程内 | 重启即丢；多副本不共享，需要外部存储才能横向扩展 |
| 调度器 | 单实例 | 多副本会重复触发；只让一个副本开启 `[scheduler] enabled` |
| 数据源启停 | 进程内 | 只影响当前副本；重启回到配置状态 |
| SSE | 单连接 | 无状态，但 `/resume` 需要能读到同一份 checkpoint；共享存储或粘性会话 |

- 连接池：当前 LLM / embedding 调用每次创建 `httpx` 客户端，连接复用有限；主要瓶颈通常是外部模型 QPS 与 `node_timeout_s`，扩容优先加副本或加网关，而不是只调大单进程参数。
- 限流：应用层没有内建限流；建议在 Nginx / API 网关按 API Key 或租户限制 RPS，并与模型供应商的配额对齐。
- 容量估算：以 SLO 的 P95 和外部模型并发上限为准，先用 `scripts/loadtest.py` 在目标环境跑 20/50 两档，再决定副本数。

---

## 6. 故障演练

```powershell
python scripts/fault_drill.py
```

- 不需要启动 API，也不依赖真实外部故障；通过注入失败适配器与不可达端点模拟。
- 报告：`data/drills/fault-drill.json`（原子替换）；标准输出打印每个场景的 `PASS/FAIL`、降级路径与总体结果。
- 退出码：全部通过为 `0`，任一场景失败为 `1`；单场景失败不影响其余场景继续执行。

当前报告（2026-11-11T05:30:26Z）四场景全部 PASS：

| 场景 | 触发 | 观察到的降级路径 |
|---|---|---|
| `model_failure` | 主 provider 500、全部 provider 500、LLM 直接抛异常 | fallback 链 `p1 → p2` 成功；全部失败抛 `httpx.HTTPError`；异常 LLM 使 Run `failed` 且步数受限 |
| `retrieval_failure` | 一个检索源抛 `TimeoutError`，另一个源正常 | 保留正常源证据，trace notes 记 `retrieve_error` / `retrieve_degraded` |
| `pg_unavailable` | Postgres DSN 指向不可达端口 | 回退内存检索 + 种子语料（`seed_chunks=14`），`health.database=down` |
| `redis_unavailable` | Redis URL 指向不可达端口 | 回退 `InMemoryCache`，读写不抛异常，`is_degraded=True` |

演练建议：交付前、升级后、依赖变更后各跑一次；报告与上一次对比，重点看降级路径是否仍成立。

---

## 7. 事故响应 checklist

1. **确认影响面**：`/health` 是否 `degraded`、`/v1/admin/metrics` 成功率与 P95、错误日志与 5xx 比例。
2. **保存现场**：复制 `.zcaisagent/audit.jsonl`、`trace.jsonl`、相关 `checkpoints/<run_id>.jsonl`、进程日志与 `data/drills/` 最近报告。
3. **止血**：
   - 网关摘流或限流；
   - 停用故障检索源（Studio 数据源页 / `PUT /v1/admin/sources/{name}`）；
   - 模型故障时确认 fallback 链已生效，必要时降低并发；
   - 卡住的审批按 3.1 处理，无法继续则取消 Run。
4. **恢复**：修复 PG / Redis / 模型 / Webhook 后重启进程（PG、Redis 连接在启动时装配），再次检查 `/health`。
5. **复盘**：按时间线整理 trace、审计、指标；确认是否有数据丢失；补充监控与阈值。
6. **回写**：把新故障模式与处置步骤补进本手册，保留演练报告作为证据。
