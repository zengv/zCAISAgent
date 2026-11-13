# 08 · Python SDK 指南

> 适用版本：M3（2026-11）｜ 客户端：`zcaisagent.sdk.ZCAISClient`
> SDK 只依赖 `httpx` 与标准库，不导入服务端 FastAPI / 适配器模块，可用于脚本、Notebook 与自动化测试。

---

## 1. 安装与导入

### 1.1 已安装服务端依赖的本地仓库

```powershell
python -m pip install -e .
```

### 1.2 不安装、直接使用源码

```powershell
$env:PYTHONPATH = "src"
```

```python
from zcaisagent.sdk import ZCAISClient
```

要求 Python 3.12+，并已安装 `httpx`（服务端核心依赖已包含）。

---

## 2. 初始化

```python
import asyncio
from zcaisagent.sdk import ZCAISClient


async def main() -> None:
    async with ZCAISClient(
        "http://127.0.0.1:8080",
        api_key="dev-key",
        timeout_s=30.0,
    ) as client:
        health = await client.health()
        print(health["status"])


asyncio.run(main())
```

构造签名：

```python
ZCAISClient(
    base_url: str,
    api_key: str = "dev-key",
    timeout_s: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
)
```

- `base_url` 末尾 `/` 会被去掉；所有请求携带 `X-API-Key`。
- `transport` 仅用于测试（如 `httpx.MockTransport`），生产保持 `None`。
- 支持 `async with`，退出时自动 `aclose()`；也可手动 `await client.aclose()`。
- 同一个客户端内部复用一个 `httpx.AsyncClient`，长驻脚本应复用它，不要每次请求新建。

---

## 3. 创建与查询 Run

### 3.1 `create_run`

```python
run = await client.create_run(
    "一线城市住宿标准是多少？",
    thread_id="thread_demo",
    tenant_id="demo",
    user_id="dev",
    level="L1",
)
print(run["run_id"], run["status"], run["answer"])
print(run["citations"])
```

签名：

```python
async def create_run(
    goal: str,
    *,
    thread_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str = "dev",
    level: str | None = None,
) -> dict
```

- `level` 可选 `L0` / `L1` / `L2` / `L3`；不传由服务端路由决定。
- 返回服务端 `RunResponse` 字典：`run_id`、`thread_id`、`status`、`stage`、`answer`、
  `confidence`、`abstained`、`citations`、`evidence_count`、`steps`、`metrics`、`approval` 等。
- `status` 可能为 `succeeded` / `failed` / `abstained` / `budget_exceeded` /
  `cancelled` / `awaiting_user`；`awaiting_user` 表示需要审批。

### 3.2 `get_run` 与 `trace`

```python
state = await client.get_run(run_id, tenant_id="demo", user_id="dev")
trace = await client.trace(run_id, tenant_id="demo", user_id="dev")

for event in trace["events"]:
    print(event["type"], event.get("node"), event["data"].get("duration_ms"))
```

签名：

```python
async def get_run(
    run_id: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> dict

async def trace(
    run_id: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> dict
```

- 两个查询都要求 Run 归属当前租户与用户；否则服务端返回 403。
- `trace()` 返回 `{"run_id": "...", "events": [...]}`；每个事件通常为 `step.finished`，
  `data` 内含节点、耗时、tokens、成本、输入/输出摘要。

---

## 4. SSE 流式对话

`chat_stream` 是异步生成器，逐个产出 `(event, data)`：

```python
async with ZCAISClient("http://127.0.0.1:8080", api_key="dev-key", timeout_s=120.0) as client:
    async for event, data in client.chat_stream("一线城市住宿标准是多少？", level="L1"):
        if event == "meta":
            print("run:", data["run_id"])
        elif event == "stage":
            print("stage:", data["node"], data["phase"])
        elif event == "token":
            print(data["text"], end="", flush=True)
        elif event == "approval_required":
            print("需要审批:", data["pending_approval"])
        elif event == "done":
            print("完成:", data["status"], data["answer"])
        elif event == "error":
            print("错误:", data["message"])
```

签名：

```python
async def chat_stream(
    goal: str,
    *,
    thread_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str = "dev",
    level: str | None = None,
) -> AsyncIterator[tuple[str, dict]]
```

- 事件类型：`meta`（含 `run_id`）、`stage`（节点开始/结束）、`token`（增量文本）、
  `approval_required`、`done`（最终答案与指标）、`error`。
- SDK 已处理 SSE 的 `event:` / 多行 `data:`、忽略注释行与 `[DONE]`；非 JSON 数据会被跳过。
- 客户端提前 `break` 或断开连接时，服务端会取消后台 Run，避免继续消耗模型调用。
- 流式请求沿用客户端的 `timeout_s`；长任务建议单独创建客户端并设为 120–300 秒。

---

## 5. 审批与取消

```python
# 同意
updated = await client.approve(run_id, reason="同意执行", actor="alice")

# 拒绝
updated = await client.reject(run_id, reason="风险过高", actor="alice")

# 取消（仅 running / awaiting_user 可取消）
cancelled = await client.cancel(run_id)
```

签名：

```python
async def approve(run_id: str, *, reason: str | None = None, actor: str | None = None) -> dict
async def reject(run_id: str, *, reason: str | None = None, actor: str | None = None) -> dict
async def cancel(run_id: str) -> dict
```

- `approve` / `reject` 仅对 `status=awaiting_user` 且 `pending_approval.status=pending` 的 Run 有效，
  否则返回 409；成功后会立即恢复执行并返回更新后的 Run。
- 如果审批已经决策但 Run 尚未继续，SDK 没有独立方法；调用服务端
  `POST /v1/runs/{run_id}/resume` 即可。
- 审计记录：`approval.approved` / `approval.rejected` / `run.cancelled`。

---

## 6. 指标与健康检查

```python
metrics = await client.metrics(window="24h")
print(metrics["requests"], metrics["latency_ms"]["p95"], metrics["quality"]["gate"])

health = await client.health()
print(health["status"])
for entry in health["entries"]:
    print(entry["name"], entry["status"], entry["message"])
```

签名：

```python
async def metrics(window: str = "24h") -> dict
async def health() -> dict
```

- `metrics` 对应 `GET /v1/admin/metrics`，需要 `admin:read` scope；`window` 支持 `30m` / `24h` / `7d` 等。
- 返回字段：`window`、`empty`、`requests`、`success_rate`、`abstain_rate`、`latency_ms`、
  `tokens`、`cost_usd`、`cache`、`quality`。
- `health` 对应 `GET /health`，不需要 admin scope；`status=degraded` 时仍返回 HTTP 200。

### 6.1 智能问数

```python
result = await client.ask_n2sql(
    "S1 的成绩是多少？",
    user_id="s_s1",
    tenant_id="demo",
)
print(result["answer"])
print(result["sql"])
print(result["scope"], result["row_count"])
for row in result["rows"]:
    print(row)
```

签名：

```python
async def ask_n2sql(
    self,
    question: str,
    *,
    user_id: str = "dev",
    tenant_id: str | None = None,
) -> dict
```

- 对应 `POST /v1/n2sql`；返回 `answer`、`sql`、`columns`、`rows`、`row_count`、`scope`、`latency_ms`。
- `user_id` 必须存在于 `config.toml` 的 `[[n2sql.users]]`，否则返回 **403**；数据范围由服务端强制注入。
- **400**：问题为空或 SQL 未通过只读/白名单校验；**502**：模型生成或 MySQL 执行失败；**503**：智能问数未启用或连接未配置。
- 聊天链路（`chat_stream` / `create_run`）命中数据查询意图时也会走同一 `n2sql` 服务，无需单独调用本接口。

---

## 7. 异常类型

| 异常 | 触发条件 | 关键属性 |
|---|---|---|
| `ZCAISError` | SDK 异常基类 | — |
| `ZCAISAPIError` | 其他非 2xx 响应 | `status_code`、`code`、`message`、`body` |
| `ZCAISAuthError` | 401，API Key 缺失或无效 | 同上 |
| `ZCAISForbiddenError` | 403，scope 不足或跨租户/跨用户 | 同上 |
| `ZCAISNotFoundError` | 404，Run 或资源不存在 | 同上 |
| `ZCAISConflictError` | 409，Run 无待审批、审批已决策、状态不可取消 | 同上 |

```python
from zcaisagent.sdk import (
    ZCAISAPIError,
    ZCAISAuthError,
    ZCAISConflictError,
    ZCAISForbiddenError,
    ZCAISNotFoundError,
)

try:
    await client.approve(run_id)
except ZCAISAuthError:
    ...  # 检查 api_key
except ZCAISForbiddenError:
    ...  # 检查租户/用户/scope
except ZCAISConflictError:
    ...  # 审批已决策或 Run 状态不允许
except ZCAISNotFoundError:
    ...  # run_id 不存在
except ZCAISAPIError as exc:
    print(exc.status_code, exc.code, exc.message)
```

> 服务端返回的 `detail` 字符串会映射到 `message`；部分接口返回 `{"code","message"}`，两者都会被解析。

---

## 8. 超时与重试建议

- **超时**：`timeout_s` 作用于连接与读取。普通 `create_run` / 查询建议 30–60 秒；
  `chat_stream` 建议 120–300 秒，避免长任务在读超时处断开。
- **重试**：SDK 不内置重试。只对网络超时、429、5xx 做指数退避重试；不要重试
  401 / 403 / 404 / 409。
- **幂等**：`create_run` 没有幂等键，超时后盲目重试可能产生重复 Run。需要重试时：
  - 使用固定的 `thread_id`，重试后查询该线程的历史确认是否已创建；
  - 或先 `get_run` 确认状态，再决定是否重新发起。
- **并发**：一个客户端可并发调用；受外部模型 QPS 与 `node_timeout_s` 约束，建议在网关层限流。
- **资源释放**：长驻进程退出前 `await client.aclose()`；使用 `async with` 时自动释放。

---

## 9. 完整示例

```python
import asyncio

from zcaisagent.sdk import ZCAISClient


async def main() -> None:
    async with ZCAISClient("http://127.0.0.1:8080", api_key="dev-key", timeout_s=120.0) as client:
        # 1. 流式对话
        run_id = None
        async for event, data in client.chat_stream("一线城市住宿标准是多少？", level="L1"):
            if event == "meta":
                run_id = data["run_id"]
            elif event == "done":
                print("答案:", data["answer"])
                print("引用:", data["citations"])

        if run_id is None:
            return

        # 2. 查询状态与轨迹
        state = await client.get_run(run_id)
        print("状态:", state["status"])
        trace = await client.trace(run_id)
        print("步数:", len(trace["events"]))

        # 3. 指标与健康
        print("健康:", (await client.health())["status"])
        print("P95:", (await client.metrics("24h"))["latency_ms"]["p95"])


asyncio.run(main())
```
