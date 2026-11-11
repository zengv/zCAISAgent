# 03 · 技术架构方案：zCAISAgent 通用 Agent（CAIS）运行时

> 版本：v1.0 ｜ 日期：2026-11-05
> 前置阅读：[01-research-cais.md](01-research-cais.md)（范式与优劣）、[02-product.md](02-product.md)（产品范围与指标）
> 本文回答：**怎么建、模块怎么切、接口怎么定、哪里可扩展、哪里必须稳定。**

---

## 0. 架构目标与非目标

### 0.1 目标

| # | 目标 | 可验证标准 |
|---|---|---|
| G1 | **一套运行时**同时支撑通用 Agent 与 Agentic RAG | 新增一个工具/检索源不改内核代码 |
| G2 | **能力可插拔** | 替换向量库/模型/重排器只改配置 + 适配器 |
| G3 | **策略可配置** | L0-L3 自主性、预算、检索深度由 YAML 决定 |
| G4 | **全链路可观测** | 任一任务可还原完整决策轨迹与成本 |
| G5 | **可评估、可门禁** | CI 中跑黄金集，指标劣化即阻断 |
| G6 | **可私有化、可水平扩展** | 单机 Compose 可跑；K8s 可扩 API/Worker |
| G7 | **安全默认开启** | 越权召回为 0；高风险工具有审批 |

### 0.2 非目标（明确不做，防止过度设计）

- ❌ 不做通用低代码工作流平台（DSL 只覆盖 Agent 图，不做任意业务流程）
- ❌ 不做模型训练/微调平台（只做推理调用与提示管理）
- ❌ MVP 不做分布式多 Agent 自由协作（只做图内多角色节点）
- ❌ 不自研向量数据库/检索引擎（用 pgvector + Postgres FTS，后续可换）
- ❌ 不追求"零框架"，但核心不依赖任何框架的内部类型

### 0.3 关键技术决策（详见 [04-engineering-plan.md](04-engineering-plan.md) ADR）

| 决策 | 选择 | 理由 |
|---|---|---|
| 语言/运行时 | Python 3.12 + asyncio | 生态、团队、模型 SDK 支持 |
| Web 框架 | FastAPI + SSE | 异步、流式、OpenAPI 原生 |
| 数据校验 | Pydantic v2 | 状态/工具 schema/配置统一 |
| 编排 | **自研轻量 Graph Runtime** + LangGraph 适配器 | 避免锁定；核心逻辑可读可测 |
| 主存储 | Postgres 16 + pgvector + FTS | 一库多用，运维简单 |
| 缓存/队列 | Redis（精确缓存 + Streams） | 轻量；MVP 可退化到进程内 |
| 对象存储 | MinIO/S3 | 原始文档与解析产物 |
| 模型接入 | OpenAI 兼容网关（LiteLLM/vLLM） | 多供应商、可私有化 |
| 工具协议 | MCP 优先 + OpenAPI 适配 | 标准化、生态 |
| 可观测 | OpenTelemetry + Langfuse/Phoenix（可选） | 厂商中立 |
| 部署 | 模块化单体 → 按需拆分 | 避免过早微服务 |

---

## 1. 架构原则（含反例）

| # | 原则 | 具体要求 | 反例（禁止） |
|---|---|---|---|
| P1 | **依赖倒置** | 内核依赖抽象（ports），适配器依赖具体 SDK | 节点里直接 `import openai` / `import psycopg` |
| P2 | **状态单一来源** | 一次运行只有 `RunState` 一个可变状态源，节点返回 patch | 节点内部维护私有状态、直接 mutate 全局 |
| P3 | **节点无副作用** | 节点只做计算与调用，不写库、不发消息；副作用走端口 | 节点里 `db.commit()` / 直接 `redis.set()` |
| P4 | **预算运行时强制** | 预算检查在执行器循环里，不在节点里"自觉" | 靠 prompt 说"不要超过 5 步" |
| P5 | **证据一等公民** | 所有外部信息统一 `Evidence`，可引用、可评分、可审计 | 检索结果直接拼字符串塞 prompt |
| P6 | **配置即策略** | 路由、预算、检索参数、模型档位来自配置 | 策略硬编码在 if/else 里 |
| P7 | **默认可观测** | 每个节点/调用自动产生 span，无需手动埋点 | 只在大流程首尾打日志 |
| P8 | **渐进式复杂** | 先单机单体，有指标证明需要才拆 | 一上来 K8s + 消息队列 + 微服务 |

> **内核与框架的边界**：核心（domain/ports/orchestration）不得 import 任何 Web、DB、LLM SDK。适配器层可以。这样单元测试不需要起容器。

---

## 2. 总体架构

### 2.1 分层视图

```mermaid
flowchart TB
    subgraph API["接入层 api/"]
        A1["POST /v1/chat (SSE)"]:::a
        A2["POST /v1/runs"]:::a
        A3["Admin / Eval / Trace API"]:::a
    end

    subgraph ORCH["编排层 orchestration/"]
        O1["GraphRuntime<br/>step / checkpoint / interrupt"]:::o
        O2["Nodes<br/>analyze / route / plan / act / observe / reflect / retrieve / grade / synthesize / verify / respond"]:::o
        O3["Policies<br/>routing / budget / termination / fallback"]:::o
    end

    subgraph CAP["能力层 capabilities/"]
        C1["retrieval<br/>registry / fusion / rerank / eval<br/>sources: inmemory·bm25·vector·pageindex·graph"]:::c
        C2["tools<br/>gateway / mcp / openapi / sql / sandbox"]:::c
        C3["memory<br/>working / episodic / semantic / procedural"]:::c
        C4["generation<br/>prompt / structured output"]:::c
    end

    subgraph PORT["端口层 ports/（抽象接口）"]
        PT["LLMPort · RetrieverPort · ToolPort · MemoryPort<br/>CheckpointPort · TracePort · EvaluatorPort"]:::p
    end

    subgraph GOV["治理层 governance/"]
        V1["Permission / Tenant / RBAC"]:::g
        V2["Guardrails / Injection Defense"]:::g
        V3["Audit / Redaction"]:::g
    end

    subgraph ADP["适配层 adapters/"]
        D1["LLM: OpenAI 兼容 / vLLM"]:::d
        D2["Store: Postgres + pgvector + FTS"]:::d
        D3["Cache: Redis"]:::d
        D4["Trace: OTel / Langfuse"]:::d
        D5["Tool: MCP client / HTTP"]:::d
    end

    subgraph DOM["领域层 domain/（无外部依赖）"]
        M1["RunState / Evidence / Answer / Plan / Budget"]:::m
        M2["Events / Errors / Enums"]:::m
    end

    API --> ORCH --> CAP --> PORT --> ADP
    ORCH --> GOV
    CAP --> GOV
    ORCH --> DOM
    CAP --> DOM
    PORT --> DOM
    ADP --> DOM

    classDef a fill:#e3f2fd,stroke:#1976d2,color:#0d47a1
    classDef o fill:#fff3e0,stroke:#ef6c00,color:#e65100
    classDef c fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef p fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    classDef g fill:#fce4ec,stroke:#c2185b,color:#880e4f
    classDef d fill:#e0f7fa,stroke:#00838f,color:#006064
    classDef m fill:#f5f5f5,stroke:#616161,color:#212121
```

### 2.2 部署视图

```mermaid
flowchart LR
    U["用户 / 业务系统"] -->|"HTTPS + SSE"| API["api (FastAPI, 无状态)"]
    API --> Q["Redis Streams<br/>任务队列"]
    Q --> W["worker (编排执行, 可多副本)"]
    W --> PG["Postgres<br/>业务表 + pgvector + 轨迹 + checkpoint"]
    W --> RD["Redis<br/>缓存 / 限流 / 锁"]
    W --> OBJ["MinIO/S3<br/>原始文档 / 解析产物"]
    W --> MG["Model Gateway<br/>LiteLLM / vLLM"]
    W --> EXT["外部工具<br/>MCP / OpenAPI / SQL / Web"]
    W --> TR["OTel Collector<br/>Langfuse / Phoenix"]
    API --> PG
```

**MVP 可简化**：API 进程内直接 `asyncio.create_task` 执行，不引入队列；接口保持不变，M2 再切 Redis Streams。

---

## 3. 运行时模型

### 3.1 核心抽象

> **实现现状**：`orchestration/runtime.py` 的 `RunContext` 为**扁平字段**（`llm/retriever/sources/reranker/checkpoint/trace/auth/config/guardrail/audit/skills/tools/memory/cache/events/evaluator/verifier/...`），**没有** `ports` 聚合对象与 `policies` 字段；预算/终止由 `BudgetPolicy` 静态方法在运行时调用。`EvaluatorPort` 有默认实现 `RuleEvaluator` 并由容器注入 `RunContext.evaluator`；`evaluator.semantic_verify_enabled` 开启时注入 `AnswerVerifierPort`（LLM NLI judge）到 `RunContext.verifier`，答案级校验由 `verify` 节点承担。

```
GraphSpec  = { nodes, edges, entry, terminals: dict[node, RunStatus] }
Node       = (RunState, RunContext) -> StatePatch
Edge       = (src, dst, condition | None)
RunState   = 不可变快照；apply(patch) 产生新状态
RunContext = { ports, config, policies, auth, trace, events, checkpoint, clock }
```

`GraphSpec.next_node(state)` 的规则：若 `state` 记录了 `last_node`，只在 `last_node` 的出边中按声明顺序求值 `condition(state)`，第一个为真的边即下一节点；无匹配则进入 `fallback`（默认 `abstain`）。`state.step` 在 `apply(patch)` 内部按 `incr_step` 单调递增，运行时不再单独调用 `bump_step`。

| `RunContext` 字段 | 内容 |
|---|---|
| `ports` | 7 个端口的实例（LLM/Retriever/Tool/Memory/Checkpoint/Trace/Evaluator） |
| `config` | 当前 agent 配置（Pydantic 模型，只读） |
| `policies` | 预算、终止、路由、降级策略实例 |
| `auth` | 租户、用户、角色、scope、ACL |
| `trace` / `events` | 追踪与事件出口 |
| `checkpoint` | 状态持久化端口 |
| `clock` | 可注入时钟（测试用，便于超时与缓存测试） |

**为什么节点返回 patch 而不是修改状态**：便于 checkpoint、回放、并发分支合并、测试断言。Patch 使用显式 reducer（覆盖 / 追加 / 合并集合），`apply()` 集中在 `orchestration/reducers.py`，节点不得自行修改状态。

**预算用量如何更新**：LLM 网关与工具网关在返回结果时，通过 `patch.budget` 回写实际 tokens/cost/耗时；运行时在每步开始执行 `budget.check(state)`。节点不能自行虚报或忽略用量。

### 3.2 执行循环（伪代码）

```python
async def execute(graph: GraphSpec, state: RunState, ctx: RunContext) -> RunState:
    while state.status == RunStatus.RUNNING:
        # 1) 运行时强制预算与终止检查（节点无权绕过）
        decision = ctx.policies.budget.check(state)
        if decision.exceeded:
            state = state.apply(StatePatch(
                status=RunStatus.BUDGET_EXCEEDED,
                error=ErrorInfo(code="BUDGET_EXCEEDED", detail=decision.reason),
            ))
            break

        # 2) 选择下一节点
        node_name = graph.next_node(state)
        if node_name in graph.terminals:
            state = state.apply(StatePatch(status=graph.terminals[node_name]))
            break

        # 3) 执行（带超时与异常归一化）
        ctx.trace.start_step(state.step, node_name)
        try:
            patch = await asyncio.wait_for(
                graph.nodes[node_name](state, ctx),
                timeout=ctx.config.node_timeout_s,
            )
        except TimeoutError:
            patch = StatePatch(status=RunStatus.FAILED, error=ErrorInfo(code="NODE_TIMEOUT"))
        except Exception as exc:
            patch = StatePatch(status=RunStatus.FAILED, error=ctx.normalize(exc))

        # 4) 合并 + 持久化 + 事件
        patch.last_node = node_name         # 运行时注入，节点不得设置
        state = state.apply(patch)          # apply 内部处理 incr_step
        await ctx.checkpoint.save(state)
        ctx.trace.end_step(state)
        ctx.events.emit(RunEvent.from_state(state))

        # 5) 中断（HITL）——状态已持久化，可恢复
        if state.status == RunStatus.AWAITING_USER:
            break

    await ctx.checkpoint.save(state)
    return state
```

### 3.3 关键运行时能力

| 能力 | 实现 | 说明 |
|---|---|---|
| **Checkpoint** | 每步 `save(run_id, state)`；现为 JSONL（`JsonlCheckpoint`，压缩+索引），可选 Postgres（`[checkpoint] backend`） | 崩溃恢复、回放、审计 |
| **Interrupt / Resume** | 状态置 `AWAITING_USER`，API 收到输入后 `resume(run_id, patch)` | HITL 审批、澄清 |
| **重试** | 仅对幂等端口（LLM/检索）自动重试；工具按 `idempotent` 标记 | 避免重复副作用 |
| **并发分支** | `asyncio.gather(..., return_exceptions=True)` 用于多源检索 | 部分失败不拖垮全局 |
| **超时** | 节点级 + 调用级双层 | 防单点卡死 |
| **取消** | `run_id` 维度的 cancellation token | 用户停止生成 |
| **幂等** | `run_id + step + node + attempt` 作为副作用键 | 重放安全 |
| **回放** | 从 checkpoint 重建状态，禁用真实 LLM（录制/回放） | 调试与回归 |

### 3.4 内置图

> **实现现状**：实际图为 `build_default_graph()`：`analyze→route→{abstain|direct_answer|retrieve|plan}`；`plan→act→observe→reflect→{act|grade}`；`retrieve→grade→{abstain|synthesize|repair}`；`synthesize→verify→{respond|abstain|repair}`；`repair→{retrieve|synthesize|respond(降级)|abstain}`。**无独立的 `rewrite`/`source_route`/`replan`/`clarify` 节点**：改写与选源在 `retrieve` 节点内部完成，重规划由 `repair` 承担，澄清未实现。下方 §3.4.1/§3.4.2 的 mermaid 为**目标态蓝图**，节点名以本节说明与 README「检索路由链路」图为准。

#### 3.4.1 通用 Agent 循环（L2/L3）

```mermaid
stateDiagram-v2
    [*] --> analyze
    analyze --> route
    route --> direct_answer: L0
    route --> plan: L1+
    plan --> act
    act --> observe
    observe --> reflect
    reflect --> plan: 继续（预算允许）
    reflect --> respond: 目标达成
    reflect --> ask_user: 需澄清/审批
    reflect --> abstain: 预算耗尽/不可恢复
    respond --> [*]
    direct_answer --> [*]
    ask_user --> [*]
    abstain --> [*]
```

`act` 从 `ActionSpace` 中选择动作：`retrieve` / `tool_call` / `code_exec` / `browse` / `file_op`；`observe` 把结果归一化为 `Observation`（可转 `Evidence`）；`reflect` 判断"目标是否达成、是否继续、是否需要人介入"。

#### 3.4.2 RAG 子图（L1/L2，通用循环的一种固定组合）

```mermaid
stateDiagram-v2
    [*] --> analyze
    analyze --> route
    route --> direct_answer: L0
    route --> rewrite: L1
    route --> plan: L2/L3
    rewrite --> source_route
    plan --> source_route
    source_route --> retrieve
    retrieve --> grade
    grade --> synthesize: 证据充足
    grade --> replan: 不足且预算允许
    grade --> clarify: 不足且需用户输入
    grade --> abstain: 不足且预算耗尽
    replan --> plan
    synthesize --> verify
    verify --> respond: 通过
    verify --> repair: 不通过且可修
    repair --> retrieve: 定向补检索
    repair --> synthesize: 改写/降级
    verify --> abstain: 不可修
    respond --> [*]
    direct_answer --> [*]
    clarify --> [*]
    abstain --> [*]
```

> RAG 子图不是独立运行时，而是通用循环在"检索为主"场景下的一种固定组合；`retrieve/grade/synthesize` 也可被通用循环当作多个 `act/observe` 步骤调用。

---

## 4. 领域模型（`domain/`）

> 原则：纯数据 + 纯函数，不依赖 DB/HTTP/SDK。Pydantic v2 仅用于校验与序列化。

```python
# domain/enums.py
from enum import StrEnum

class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABSTAINED = "abstained"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"

class AutonomyLevel(StrEnum):
    L0_DIRECT = "L0"
    L1_FAST = "L1"
    L2_STANDARD = "L2"
    L3_DEEP = "L3"

class SourceType(StrEnum):
    DOCUMENT = "document"
    WEB = "web"
    API = "api"
    SQL = "sql"
    TOOL = "tool"
    MEMORY = "memory"

class Support(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
```

```python
# domain/models.py
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

class Citation(BaseModel):
    evidence_id: str
    quote: str | None = None
    start: int | None = None
    end: int | None = None
    confidence: float = 0.0

class Evidence(BaseModel):
    id: str
    source_type: SourceType
    source_name: str                     # 检索器/工具名，如 "kb_vector" / "crm_api"
    title: str | None = None
    uri: str | None = None               # 可点击来源
    content: str                         # 进入上下文的正文
    snippet: str | None = None           # 展示用高亮片段
    score: float = 0.0                   # 融合/重排后的分数
    relevance: float = 0.0
    authority: Literal["primary", "secondary", "unknown"] = "unknown"
    published_at: datetime | None = None
    retrieved_at: datetime
    version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    citation: Citation | None = None

class Claim(BaseModel):
    id: str
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    support: Support = Support.UNSUPPORTED
    note: str | None = None

class Answer(BaseModel):
    text: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    abstained: bool = False
    limitations: list[str] = Field(default_factory=list)

class SubQuestion(BaseModel):
    id: str
    text: str
    depends_on: list[str] = Field(default_factory=list)
    source_hints: list[str] = Field(default_factory=list)
    status: Literal["pending", "retrieving", "covered", "failed"] = "pending"

class PlanStep(BaseModel):
    id: str
    kind: Literal["retrieve", "tool", "reason", "ask_user"]
    target: str                          # source_name 或 tool_name
    query: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "done", "failed", "skipped"] = "pending"

class Plan(BaseModel):
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    version: int = 1

class Budget(BaseModel):
    max_steps: int = 8
    max_llm_calls: int = 12
    max_tokens: int = 60_000
    max_cost_usd: float = 0.15
    max_wall_time_s: int = 90
    used_steps: int = 0
    used_llm_calls: int = 0
    used_tokens: int = 0
    used_cost_usd: float = 0.0
    deadline_ts: float = 0.0

class Verdict(BaseModel):
    passed: bool
    faithfulness: float = 0.0
    citation_support: float = 0.0
    issues: list[str] = Field(default_factory=list)
    suggested_action: Literal["respond", "repair_retrieve", "repair_rewrite", "abstain"] = "respond"

class RunState(BaseModel):
    run_id: str
    thread_id: str
    tenant_id: str
    user_id: str
    goal: str                            # 原始用户输入，永不覆盖
    level: AutonomyLevel = AutonomyLevel.L1_FAST
    messages: list[dict[str, Any]] = Field(default_factory=list)
    plan: Plan | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    working_notes: list[str] = Field(default_factory=list)   # 模型中间结论（可压缩）
    answer: Answer | None = None
    verdict: Verdict | None = None
    budget: Budget = Field(default_factory=Budget)
    step: int = 0
    last_node: str | None = None          # 由运行时注入，用于确定出边
    repair_round: int = 0
    status: RunStatus = RunStatus.RUNNING
    error: dict[str, Any] | None = None
    schema_version: int = 1
```

### 4.1 状态 Patch 与 Reducer

```python
# domain/patch.py
from dataclasses import dataclass, field
from typing import Any

@dataclass
class StatePatch:
    status: RunStatus | None = None
    level: AutonomyLevel | None = None
    plan: Plan | None = None
    answer: Answer | None = None
    verdict: Verdict | None = None
    budget: Budget | None = None
    error: dict[str, Any] | None = None
    append_evidence: list[Evidence] = field(default_factory=list)
    replace_evidence: list[Evidence] | None = None
    append_notes: list[str] = field(default_factory=list)
    append_message: dict[str, Any] | None = None
    incr_step: int = 0
    incr_repair: int = 0
    last_node: str | None = None          # 运行时注入，节点不应自行设置
```

Reducer 规则（必须显式，避免隐式行为）：

| 字段 | 规则 |
|---|---|
| `evidence` | 默认追加；同 `id` 覆盖；`replace_evidence` 全量替换（用于"清空重检"） |
| `plan` | 整体替换（版本号 +1） |
| `messages` | 追加；超过 token 预算由 `ContextPacker` 压缩 |
| `budget` | 整体替换（由模型网关/工具网关回报实际用量） |
| `step` | 单调递增，由运行时维护 |

---

## 5. 端口契约（`ports/`）

> 端口是**唯一**允许跨层依赖的抽象。适配器实现端口，内核只依赖端口。

```python
# ports/llm.py
from collections.abc import AsyncIterator
from typing import Protocol

class LLMPort(Protocol):
    async def complete(self, req: "LLMRequest") -> "LLMResponse": ...
    def stream(self, req: "LLMRequest") -> AsyncIterator["LLMDelta"]: ...
```

```python
# ports/retrieval.py
class RetrieverPort(Protocol):
    source_name: str
    source_type: SourceType
    async def retrieve(self, req: "RetrieveRequest", ctx: "AuthContext") -> "RetrieveResult": ...
```

```python
# ports/tools.py
class ToolPort(Protocol):
    spec: "ToolSpec"
    async def invoke(self, args: dict, ctx: "AuthContext") -> "ToolResult": ...
```

```python
# ports/memory.py
class MemoryPort(Protocol):
    async def read(self, query: "MemoryQuery", ctx: "AuthContext") -> list["MemoryItem"]: ...
    async def write(self, item: "MemoryItem", ctx: "AuthContext") -> None: ...
```

```python
# ports/checkpoint.py
class CheckpointPort(Protocol):
    async def save(self, state: "RunState") -> None: ...
    async def load(self, run_id: str) -> "RunState | None": ...
    async def list_thread(self, thread_id: str) -> list["RunState"]: ...
```

```python
# ports/trace.py
class TracePort(Protocol):
    def start_step(self, step: int, node: str) -> None: ...
    def end_step(self, state: "RunState") -> None: ...
    def span(self, name: str, **attrs) -> "AbstractContextManager": ...
    def event(self, event: "RunEvent") -> None: ...
```

```python
# ports/evaluation.py
@runtime_checkable
class EvaluatorPort(Protocol):
    def grade_evidence(self, question: str, evidence: list[Evidence],
                       rerank_scores: dict[str, float] | None = None) -> list[EvidenceGrade]: ...
    def judge_sufficiency(self, question: str, evidence: list[Evidence],
                          grades: list[EvidenceGrade]) -> Sufficiency: ...
    # 默认实现：orchestration/policies/evidence.py::RuleEvaluator；答案级校验在 verify 节点。
```

### 5.1 端口设计约束

| 约束 | 说明 |
|---|---|
| **签名稳定** | 端口方法一旦发布，只增不改；破坏性变更走 v2 |
| **不泄漏实现类型** | 不返回 SDK 对象、不暴露 SQL/HTTP 细节 |
| **可注入 context** | `AuthContext` 必传，权限过滤在适配器内完成 |
| **可单测** | 所有端口提供内存假实现（`tests/fakes/`） |
| **错误归一化** | 适配器把供应商错误映射为领域错误码 |

---

## 6. 事件与追踪契约

> **实现现状**：`RunEvent{type,run_id,step,node,span?,parent_id?,data,ts}`。`observability.emit_events`（默认开）时运行期发射 `run.started/step.started/step.finished/llm.called/retrieval.performed/rerank.performed/evidence.graded/tool.called/action.performed/answer.verified/run.finished` 等细粒度事件写入 trace（检索按源聚合，避免洪泛）；EventSink（Webhook）仍只投递高层生命周期事件。OTel 见 §15。

### 6.1 事件类型

| 事件 | 触发点 | 关键字段 |
|---|---|---|
| `run.started` | 运行开始 | run_id, tenant, level, goal_hash |
| `step.started` / `step.finished` | 节点执行 | step, node, latency_ms |
| `llm.called` | 模型调用 | model, tokens_in/out, cost, cache_hit, purpose |
| `retrieval.performed` | 检索 | source, query_hash, top_k, hits, latency, degraded |
| `evidence.graded` | 证据评分 | evidence_ids, relevance, sufficiency |
| `tool.called` | 工具调用 | tool, risk, args_hash, status, latency |
| `approval.requested` / `approval.resolved` | HITL | action, approver, decision |
| `answer.verified` | 校验 | faithfulness, citation_support, action |
| `run.finished` | 结束 | status, steps, tokens, cost, latency, resolved_flag |
| `feedback.received` | 用户反馈 | rating, corrected_answer, trace_id |

### 6.2 追踪字段（最小集）

```json
{
  "trace_id": "trc_...",
  "run_id": "run_...",
  "tenant_id": "t_...",
  "step": 3,
  "node": "retrieve",
  "span": "retrieval.kb_vector",
  "latency_ms": 412,
  "tokens_in": 0,
  "tokens_out": 0,
  "cost_usd": 0.0,
  "cache_hit": false,
  "attributes": { "source": "kb_vector", "top_k": 12, "hits": 8, "degraded": false },
  "evidence_ids": ["ev_1", "ev_2"]
}
```

> 用 OpenTelemetry `gen_ai.*` 语义约定命名 LLM span，便于接 Langfuse/Phoenix/Grafana，不用自造一套。

---

## 7. 主流程时序（L2 · RAG 子图）

> 通用动作循环（`plan → act → observe → reflect`）与此同构，只是把 `retrieve` 换成 `tool/code/browse/file`。下图为检索型任务的完整链路。

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant API as API
    participant RT as Runtime
    participant AN as analyze/route
    participant PL as plan
    participant SR as source_route
    participant RE as retrieve(多源并行)
    participant GR as grade/sufficiency
    participant SY as synthesize
    participant VE as verify
    participant DB as Postgres
    participant TR as Trace

    U->>API: POST /v1/chat (SSE)
    API->>RT: create run(state)
    RT->>DB: checkpoint(save)
    RT->>AN: analyze + route
    AN-->>RT: patch(level=L2, need_retrieval=true)
    RT->>PL: plan(子问题/检索计划)
    PL-->>RT: patch(plan)
    RT->>SR: 源打分 + 权限过滤
    SR-->>RT: patch(source plan)
    par 多源并行
        RT->>RE: kb_vector.retrieve
        RT->>RE: web.retrieve
    end
    RE-->>RT: patch(evidence 追加)
    RT->>GR: 评分 + 充分性
    GR-->>RT: patch(sufficiency=不足)
    alt 预算允许
        RT->>PL: replan（补检索/换源）
    else 预算耗尽
        RT-->>API: abstain
    end
    RT->>SY: 证据约束生成
    SY-->>RT: patch(answer + claims + citations)
    RT->>VE: claim 级校验
    alt 通过
        VE-->>RT: patch(verdict.passed=true)
        RT-->>API: 流式输出 + 引用
    else 不通过
        VE-->>RT: patch(suggested_action=repair_retrieve)
        RT->>RE: 定向补检索（消耗预算）
    end
    RT->>DB: checkpoint(final)
    RT->>TR: run.finished
    API-->>U: 答案卡片（含引用/置信度/局限）
```

---

## 8. 检索子系统

### 8.1 索引管线

```
接入 → 解析 → 清洗 → 结构识别 → 分块 → 元数据增强 → 向量化 → 写入 → 校验
```

| 阶段 | 要点 | 失败处理 |
|---|---|---|
| 解析 | PDF（含表格/OCR）、DOCX、HTML、MD、CSV | 单文件失败隔离，标记 `parse_failed` |
| 清洗 | 去页眉页脚/导航/重复模板 | 保留原文 hash 用于追溯 |
| 结构识别 | 标题层级、段落、表格、代码块 | 作为 chunk 边界与父子块依据 |
| 分块 | 结构感知；512–800 tokens；overlap 15% | 超长表格单独成块并保留表头 |
| 元数据 | 租户、ACL、密级、生效/失效时间、版本、来源、语言 | 缺失则拒绝入索引（防越权） |
| 向量化 | 批量、可重试、幂等（doc_hash + chunk_idx） | 失败重入队列 |
| 写入 | pgvector + FTS 同事务 | 版本切换用别名/版本号，原子生效 |

### 8.2 查询理解

```python
class QueryAnalysis(BaseModel):
    intent: Literal["chitchat", "lookup", "compare", "howto", "analysis", "unsafe"]
    complexity: Literal["simple", "multi_hop", "open"]
    freshness: Literal["none", "recent", "realtime"]
    needs_tools: bool
    needs_clarification: bool
    entities: list[str]
    language: str
```

- **规则优先**：正则/词典处理编号、代码、精确标题 → 标记 `no_rewrite`；
- **模型兜底**：小模型分类 + 置信度；低于阈值走保守策略（L1 + 不改写）；
- **用户覆盖**：UI 可强制"快速/深度"，优先于模型判断。

### 8.3 改写策略分级

| 策略 | 触发 | 实现 | 风险控制 |
|---|---|---|---|
| `none` | 精确查询、代码、编号、短问句 | 原样 | — |
| `light` | 口语化、含指代 | 去寒暄 + 指代消解（基于 working memory） | 保留原始 query 作为并列检索项 |
| `multi_query` | 语义模糊、多义 | 生成 2–3 个变体 | 原 query 必须保留 |
| `hyde` | 召回极低且预算允许 | 生成假设答案再检索 | 仅作检索用，不进入最终上下文 |
| `decompose` | 多跳/对比 | 拆子问题（≤ 5 个） | 子问题需回指原问题 |

> **硬规则**：原始 query 永远参与检索（至少一路），改写结果不能替代它。这直接修复图中第 1 步的缺陷。

### 8.4 混合检索与融合

```
score = RRF( vector_rank, lexical_rank ) = Σ 1 / (k + rank_i),  k = 60
```

| 组件 | 实现 | 参数 |
|---|---|---|
| 向量检索 | pgvector HNSW，cosine | `top_k=30` |
| 关键词检索 | Postgres FTS / OpenSearch BM25 | `top_k=30` |
| 过滤 | tenant + acl + time + version + lang | 检索前执行（非后置） |
| 融合 | RRF + 去重（内容 hash + 相似度 > 0.95） | — |
| 重排 | cross-encoder（bge-reranker-v2-m3 / API） | `top_n=8` |
| 上下文打包 | 父子块扩展 + token 预算裁剪 | `max_context_tokens` 可配 |

**为什么检索前过滤**：后置过滤会导致"先召回越权文档再丢弃"，既浪费又可能泄漏到日志。ACL 必须进入查询条件。

#### 8.4.1 当前实现状态（2026-11-11 M1 收口复核）

本节设计面向生产规模（pgvector + Postgres FTS + cross-encoder）；当前实现为零
外部依赖的轻量等价物，接口契约一致，存储可按本节设计平滑升级：

| 设计组件 | 当前实现 | 差异说明 |
|---|---|---|
| 向量检索（pgvector HNSW） | `sources/vector.py`：OpenAI 兼容 `/embeddings` + 内存余弦，懒构建；`sources/pgvector.py`：pgvector HNSW 持久化检索 | 默认内存；启用 `[database]` 后走 PG，连接失败自动回退内存 |
| 关键词检索（Postgres FTS/OpenSearch） | `sources/bm25.py`：SQLite FTS5，中文单字+二元组展开 | 单机内存索引 |
| 融合（RRF） | `fusion.py rrf_fuse`：支持 `weights` 源权重（主源 1.0，结构化补充源 0.5） | 多源并发 + 内容去重合并 |
| 重排（cross-encoder） | `rerank.py`：优先专用端点（`style="dashscope"` 原生 / `"cohere"` 兼容），`FallbackReranker` 自动回退 embedding 余弦 → LLM 打分 | 评测 NDCG@10 0.9525（golden 100 案例） |
| 查询改写（HyDE） | `rewrite.py hyde_variant`：`open` 复杂度生成假设性答案参与召回，失败/超时跳过 | 默认关闭（`hyde_enabled`），需额外 LLM 预算 |
| 上下文打包（父子块） | `ingestion/chunker.py` 子块携带父文 + `context_pack.py` 按 `max_context_tokens` 裁剪 | 默认关闭（`parent_child_enabled`），开启后需重跑检索评测 |
| 检索源扩展 | `SourceRegistry` priority + enabled 开关；额外源：`pageindex`（章节树导航）、`graph`（实体关系图+补抽+磁盘缓存）——消融显示融入常规链路为负贡献，默认停用 | — |
| 评测与门禁 | `evaluation/retrieval.py` + `retrieval-eval` CLI：Recall@5/@10、MRR、NDCG@10，doc/answer 双口径；`evaluation/gate.py` + `eval-gate` CLI + CI workflow | 当前最优 doc R@10=0.9971 |

过滤与版本控制已在各源内实现（tenant + ACL + 版本过滤先于打分），与"检索前过滤"原则一致。

### 8.5 证据评分（四维）

| 维度 | 信号 | 权重（默认） |
|---|---|---|
| Relevance | rerank score 归一化 | 0.45 |
| Authority | 一手/二手/未知；官方源加分 | 0.25 |
| Freshness | 发布时间衰减（按场景配置半衰期） | 0.15 |
| Consistency | 与其他证据的一致/冲突 | 0.15 |

```python
class EvidenceGrade(BaseModel):
    evidence_id: str
    relevance: float
    authority: float
    freshness: float
    consistency: float
    final: float
    decision: Literal["keep", "drop", "conflict"]
```

### 8.6 充分性判断（与 relevance 分离）

> **实现现状**：`orchestration/policies/evidence.py::judge_sufficiency` 目前**仅用规则**：单问题建模，`coverage∈{0,1}`（至少一条 `final≥0.40` 且主题二元组命中即 1），`coverage<0.7` 判 `insufficient`。**未接入模型 judge**，也未感知子问题（待 FR-LOOP-01 落地）。decision 实际取值为 `sufficient/insufficient/conflict`（非本节的 `conflicting`）。

```python
class Sufficiency(BaseModel):
    covered_sub_questions: list[str]
    uncovered_sub_questions: list[str]
    coverage: float                 # 0-1
    missing_aspects: list[str]
    decision: Literal["sufficient", "insufficient", "conflicting"]
```

判定融合三类信号：
1. **规则**：每个子问题至少 1 条 `final >= threshold` 的证据；
2. **覆盖**：claim 草稿中的关键实体/维度是否都有对应证据；
3. **模型**：小模型 judge 输出缺口清单（需结构化输出 + 置信度）。

**默认阈值**：`coverage >= 0.7` 且无 `unsupported` claim 才进入生成；否则 `replan`（受预算约束）。

### 8.7 缓存与增量

| 缓存 | 键 | 失效 |
|---|---|---|
| 检索结果缓存 | `hash(normalized_query + filters + source + top_k)` | TTL 5 min（`cache.ttl_s=300`，默认关闭）；文档版本变更时按前缀失效 |
| 语义缓存 | embedding 近邻（阈值 0.97） | 仅对 L1 高频问题开启；答案需带"缓存命中"标记 |
| 重排缓存 | `hash(query + evidence_ids)` | 随证据变化 |
| 文档解析缓存 | `doc_hash` | 文档变更 |

**增量索引**：定时/事件触发；`doc_hash` 未变则跳过；删除走软删除 + 索引 tombstone。

---

## 9. 工具子系统

### 9.1 工具描述契约

```python
class ToolSpec(BaseModel):
    name: str
    description: str                     # 给模型看：做什么、何时用、不何时用
    input_schema: dict                   # JSON Schema
    output_schema: dict | None = None
    risk_level: Literal["read", "write", "destructive"]
    scopes: list[str]                    # 需要的权限
    timeout_s: int = 20
    idempotent: bool = False
    requires_approval: bool = False
    max_output_chars: int = 8_000
    rate_limit: str | None = None        # 如 "10/min"
```

### 9.2 调用流水线

```mermaid
flowchart LR
    A["模型请求工具"] --> B["Gateway.resolve<br/>是否存在"]
    B --> C["authorize<br/>scope + tenant"]
    C --> D["validate<br/>JSON Schema"]
    D -->|"高风险"| E["approval<br/>interrupt"]
    E --> F["invoke<br/>timeout / retry / 熔断"]
    D --> F
    F --> G["sanitize<br/>截断 / 脱敏 / 注入检测"]
    G --> H["audit + 返回 Evidence"]
```

| 环节 | 规则 |
|---|---|
| **resolve** | 只查已注册工具；禁止模型自由拼 URL/命令 |
| **authorize** | 用户 scope ∩ 工具 scope；租户隔离 |
| **validate** | JSON Schema 严格校验（`additionalProperties=false`） |
| **approval** | `risk_level != read` 或 `requires_approval` → `AWAITING_USER` |
| **invoke** | 超时、指数退避、熔断（连续失败 5 次打开 60s） |
| **sanitize** | 输出截断、PII 脱敏、注入模式检测 |
| **audit** | 参数 hash + 结果摘要 + 审批人，append-only |

### 9.3 错误码（统一）

| 码 | 含义 | Agent 可采取的动作 |
|---|---|---|
| `TOOL_NOT_FOUND` | 未注册 | 换工具或告知用户 |
| `TOOL_UNAUTHORIZED` | 无权限 | 请求授权/降级 |
| `TOOL_INVALID_ARGS` | 参数不合法 | 修正参数重试（≤2 次） |
| `TOOL_TIMEOUT` | 超时 | 重试或换源 |
| `TOOL_RATE_LIMITED` | 限流 | 退避或换源 |
| `TOOL_FAILED` | 执行失败 | 记录并降级 |
| `TOOL_OUTPUT_TOO_LARGE` | 输出过大 | 截断/分页 |
| `TOOL_INJECTION_SUSPECTED` | 输出可疑 | 隔离，不作为指令 |

### 9.4 接入方式

| 方式 | 适用 | 实现要点 |
|---|---|---|
| **MCP** | 标准工具/资源 | client 生命周期管理；工具列表缓存；schema 映射 |
| **OpenAPI** | 业务系统 REST | OpenAPI → ToolSpec 自动生成；只允许白名单 operationId |
| **SQL** | 结构化数据 | 只读账号；参数化；表白名单；结果行数上限 |
| **Browser** | 网页/需登录 | 独立沙箱；域名白名单；禁止执行任意脚本 |
| **File** | 文件读写/转换/打包 | 路径白名单；禁止跳出工作目录；全量审计 |
| **Code** | 数据分析 | 容器沙箱；无网络；CPU/内存/时间限制 |

---

## 10. 记忆子系统

### 10.1 四类记忆

| 类型 | 内容 | 存储 | 生命周期 | 检索方式 |
|---|---|---|---|---|
| **Working** | 当前 run 的消息、计划、证据摘要 | RunState + 摘要 | 单次运行 | 直接注入 |
| **Episodic** | 历史会话、决策、反馈 | Postgres | 90 天（可配） | 按 thread/user 召回 |
| **Semantic** | 用户/租户偏好、稳定事实 | Postgres KV + 向量 | 长期 | 向量 + 精确键 |
| **Procedural** | 成功计划模板、工具使用套路 | Postgres + 向量 | 长期 | 按任务类型召回 |

### 10.2 写入策略（防止记忆污染）

| 来源 | 策略 |
|---|---|
| 用户显式"记住…" | 直接写 Semantic，需脱敏与确认 |
| 任务结束蒸馏 | 生成候选记忆 → 规则过滤 → 低风险自动写入，高风险待审核 |
| 反馈纠错 | 写入 Episodic + 作为评估样本，不直接改 Semantic |
| 工具/网页内容 | **禁止**写入长期记忆（不可信源） |

### 10.3 上下文预算

```
system + 最近 N 轮 + 记忆摘要 + 证据 + 当前问题 ≤ max_context_tokens
```

- 证据优先保留高 `final` 分 + 覆盖未覆盖子问题的条目；
- 超出时先压缩 `working_notes`，再裁剪低分证据；
- 永远保留原始 `goal` 与最近一轮用户输入。

---

## 11. 编排策略

### 11.1 路由（L0-L3）

> **口径澄清（实现现状）**：本表「最大有效动作次数」现在是**运行时硬上限**（`Budget.max_actions`，取值同本表 1/3/8/15）同时用于评测；运行时与节点步数（`budget.py` 的 `max_steps`：L0=4 / L1=16 / L2=22 / L3=32）**并行强制**。PRD 的「平均有效动作 ≤2/5/12」是评测目标均值，与此硬上限不同。见 PRD FR-LOOP-05 / FR-AI-08。

| 等级 | 触发条件（任一） | 动作 | 最大有效动作次数 | 典型延迟 |
|---|---|---|---|---|
| L0 | 闲聊/格式化/翻译；缓存直接命中 | 无 | 1 | < 3s |
| L1 | 单步任务：一次检索或一次只读工具 | 1 | 3 | < 8s |
| L2 | 多步任务：跨源检索、多个工具、文件处理 | ≤ 8 个动作 | 8 | < 25s |
| L3 | 开放任务：深度调研、代码/浏览/文件组合 | ≤ 15 个动作 | 15 | < 90s |

> **口径说明**：本表按**有效动作次数**计（`retrieve`/`tool_call`/`code_exec`/`browse`/`file_op`/`ask_user` 每执行一次计 1 次）。节点执行次数（`analyze`/`route`/`plan`/`grade`/`verify` 等控制节点也计 1）是另一套口径，只用于预算硬上限，见 05 FR-LOOP-05。两者不得混用。

**成本分层**：L0/L1 用小模型；L2 中模型；L3 大模型 + 小模型做子任务。路由错误时允许用户一键升级/降级。

### 11.2 计划与重规划

> **实现现状**：`plan` 节点用**确定性规则** `rewrite_queries` 拆子查询（`multi_*`/`decompose_*`，L2 上限 2、L3 上限 5），**未**用 LLM 结构化输出 `SubQuestion[]`；`Plan` 模型也只有 `steps`（无 `sub_questions`/依赖关系）。

- **分解**：LLM 结构化输出 `SubQuestion[]`，数量上限 5；依赖关系显式；
- **计划**：子问题 → `PlanStep`（retrieve/tool/reason/ask_user）；
- **重规划触发**：充分性不足、工具失败、新信息出现、用户补充；
- **重规划约束**：只允许修改未完成步骤；已完成步骤的结果保留在证据池；
- **禁止**：无新证据的"空转重规划"（用 evidence hash 检测 no-progress）。

### 11.3 验证与修复

> **实现现状**：默认用**词面近似**（`overlap_ratio≥0.55` + 数字/日期复算 + 词元覆盖率）。`evaluator.semantic_verify_enabled=true` 时改由 `LLMEntailmentJudge`（经 `LLMPort`，即 `ctx.verifier`）逐 claim 判定蕴含：`score≥min_entailment 且数字校验通过` 才算 SUPPORTED；默认关、失败按 `fail_open` 回退词面、judge 用量计入预算。

```python
class Verdict(BaseModel):
    passed: bool
    faithfulness: float
    citation_support: float
    issues: list[str]
    suggested_action: Literal["respond", "repair_retrieve", "repair_rewrite", "abstain"]
```

校验三层：

| 层 | 方法 | 通过标准 |
|---|---|---|
| **结构** | 每条 claim 必须有 evidence_id；引用必须存在 | 100% |
| **语义** | NLI / LLM judge：claim 是否被证据蕴含 | ≥ 0.8 |
| **一致性** | 数字/日期/专有名词与证据比对；证据间冲突检测 | 无硬冲突 |

修复策略优先级：`补检索 > 改写 claim > 降低置信度并标注 > 拒答`。每次修复消耗预算，最多 2 轮。

### 11.4 终止策略

| 条件 | 动作 |
|---|---|
| 预算耗尽 | 返回当前最佳答案 + 明确"未完成项"，或 `abstain` |
| 连续 2 轮无新增有效证据 | 停止并输出局限 |
| 用户取消 | `CANCELLED`，保留部分结果 |
| 验证通过 | 正常结束 |
| 不可恢复错误 | `FAILED` + 错误码 |

---

## 12. 模型网关

> **实现现状**：模型层不使用 `small/medium/large` 档，而是 **provider 绑定 `reasoning_effort`（low/high/max）+ `[purpose_effort]` 映射 + 邻近等级双向降级**，配置在 `config.toml`，见 §17。下表 yaml 为早期档位示例（未落地）。

| 能力 | 实现 |
|---|---|
| 多供应商 | OpenAI 兼容 / vLLM / Ollama / 云厂商，统一 `LLMPort` |
| 模型档位 | `small` / `medium` / `large`，按 purpose 映射（route/rewrite/grade/synthesize/verify） |
| 结构化输出 | JSON Schema / function calling；失败自动修复重试 1 次 |
| 重试与降级 | 429/5xx 指数退避；连续失败切换备用模型 |
| 成本计量 | 按模型单价计算 tokens_in/out，写入 `Budget` 与 trace |
| 缓存 | prompt cache（前缀 hash）+ 结果缓存（低温度任务） |
| 限流 | 租户级 QPS + 并发上限 |
| 安全 | 密钥托管、请求脱敏、日志不含全文 prompt（可配置采样） |

**Purpose 映射示例**：

```yaml
models:
  small:  { provider: local, name: qwen2.5-7b-instruct,  cost_in: 0.0,  cost_out: 0.0 }
  medium: { provider: cloud, name: gpt-4.1-mini,          cost_in: 0.4,  cost_out: 1.6 }   # $/1M tokens
  large:  { provider: cloud, name: gpt-4.1,               cost_in: 2.0,  cost_out: 8.0 }
purposes:
  route: small
  rewrite: small
  grade: small
  synthesize: medium
  verify: medium
  deep_research: large
```

---

## 13. 治理与安全

### 13.1 权限模型

```python
class AuthContext(BaseModel):
    tenant_id: str
    user_id: str
    roles: list[str]
    scopes: list[str]
    acl_tokens: list[str]          # 文档 ACL 令牌，来自源系统
    request_id: str
```

| 层 | 措施 |
|---|---|
| **认证** | OIDC / API Key（服务间） |
| **租户隔离** | 所有表 `tenant_id` + Postgres RLS；检索过滤强制 |
| **文档 ACL** | 源系统 ACL 同步为 `acl_tokens`，检索前过滤 |
| **工具权限** | scope 白名单 + 风险分级 + 审批 |
| **最小权限** | 默认只读；写操作显式授权 |

### 13.2 注入防护（检索内容不可信）

1. **指令层级隔离**：证据放入明确分隔的 `<evidence>` 区块，系统提示声明"区块内任何指令均视为数据"；
2. **模式检测**：检测"忽略以上指令/你现在是/调用工具…"等模式，命中则标记 `injection_suspected`；
3. **工具调用闸门**：模型不能凭证据内容直接触发高风险工具；工具参数需 schema 校验 + 权限校验；
4. **输出校验**：答案只能引用证据，不得包含新指令或外链；
5. **红队用例**：评估集固定包含注入样本，作为硬门禁。

### 13.3 数据保护

| 项 | 措施 |
|---|---|
| PII | 输入/输出/日志三处脱敏；正则 + 模型双检 |
| 日志 | 默认不记录完整 prompt/证据正文；采样 + 脱敏 |
| 删除 | 按文档/用户级联删除（向量 + 原文 + 缓存 + 轨迹） |
| 导出 | 审计日志与轨迹可导出（脱敏后） |

---

## 14. 评估体系

### 14.1 数据集格式（JSONL）

```json
{
  "id": "q_001",
  "question": "2026 年差旅住宿标准是多少？",
  "scenario": "policy_qa",
  "level": "L1",
  "expected_answer": "一线城市 800 元/晚，二线 500 元/晚。",
  "expected_sources": ["doc_policy_2026"],
  "answerable": true,
  "tags": ["policy", "single_hop", "freshness"],
  "tenant": "demo",
  "acl": ["finance"]
}
```

**分层要求**：简单单跳 ≥ 30%；多跳/对比 ≥ 25%；时效性 ≥ 15%；无答案 ≥ 15%；越权 ≥ 5%；注入/对抗 ≥ 10%。

### 14.2 指标

| 类别 | 指标 | 计算 |
|---|---|---|
| 端到端 | Answer Correctness | 双 judge + 人工抽检 5% |
| 端到端 | Faithfulness | Ragas |
| 端到端 | Citation Support | claim 有据率（自研） |
| 检索 | Context Recall / Precision | Ragas / 标注 |
| 检索 | NDCG@5 | 重排评估 |
| 轨迹 | Steps / Invalid Tool Rate / Redundant Retrieval Rate | trace 统计 |
| 效率 | P50/P95 latency、tokens、cost | trace |
| 安全 | 越权召回率、注入通过率、PII 泄漏率 | 专项集 |
| 稳定性 | 同题多次运行方差 | 重复 5 次 |

### 14.3 门禁（CI）

```bash
pytest tests/evals -m regression --baseline=baseline.json --fail-on-drop
```

| 规则 | 阈值 |
|---|---|
| 总分下降 | > 2% 阻断 |
| Faithfulness | < 0.85 阻断 |
| Citation Support | < 0.85 阻断 |
| 越权召回 | > 0 阻断 |
| 注入通过率 | < 90% 阻断 |
| P95 延迟 | 超基线 20% 警告 |

> 评估器本身也要校准：双 judge 不一致率 > 15% 时触发人工复核，防止"评估者悖论"。

---

## 15. 可观测性

### 15.1 Span 树

> **实现现状**：默认 `JsonlTrace` 写节点级 step 与细粒度事件；`observability.otel_enabled=true` 且安装可选依赖 `.[otel]` 时使用 `OtelTrace`，把步骤映射为 span、事件作为 span event；初始化失败自动回退 JSONL（`health.trace` 标记）。

```
run (run_id)
├── step analyze
│   └── llm.call (purpose=route)
├── step retrieve
│   ├── retrieval.kb_vector
│   ├── retrieval.web
│   └── rerank
├── step synthesize
│   └── llm.call (purpose=synthesize, stream)
└── step verify
    └── llm.call (purpose=verify)
```

### 15.2 必备看板

| 看板 | 内容 |
|---|---|
| 质量 | 分层正确率、faithfulness、引用支持率、拒答准确率 |
| 轨迹 | 有效动作次数与节点步数分布、重规划率、无效调用率、失败节点 Top N |
| 成本 | 每任务成本、按租户/模型/purpose 切片 |
| 延迟 | 首 token、各节点、P95/P99 |
| 安全 | 越权拦截、注入命中、审批拒绝率 |

### 15.3 告警

- 单租户小时成本超预算 2 倍；
- 错误率 > 3%（5 分钟窗口）；
- P95 延迟超 SLO 50%；
- 越权召回 > 0（立即）；
- 注入拦截率突增（可能遭攻击）。

---

## 16. 部署拓扑

### 16.1 开发/试点（Docker Compose）

```yaml
services:
  api:        # FastAPI + SSE
  worker:     # 编排执行（MVP 可与 api 合并）
  postgres:   # pgvector + FTS + 业务表
  redis:      # 缓存 / 队列 / 锁
  minio:      # 原始文档
  gateway:    # LiteLLM（可选）
  langfuse:   # 可观测（可选）
```

### 16.2 生产（K8s）

| 组件 | 扩缩容依据 | 说明 |
|---|---|---|
| `api` | CPU / RPS | 无状态，SSE 长连接需注意连接数 |
| `worker` | 队列深度 / 任务延迟 | 有状态仅通过 checkpoint |
| `postgres` | 垂直 + 读副本 | pgvector 索引与业务表同库，注意 IO |
| `redis` | 内存 | 缓存与队列分离实例更稳 |
| `gateway` | 模型 QPS | 统一出口，便于限流与审计 |

**拆分顺序**（仅在有指标证明时）：索引 worker → 检索服务 → 评估服务。不要先拆编排器。

---

## 17. 配置契约

> **实现现状（2026-11-11 复核）**：真实配置为项目根 `config.toml`（TOML，`tomllib` 解析），由
> `settings.py` 读取 `.env` + `config.toml`（环境变量优先）并注入。**早期设计中的
> `configs/agents/general_assistant.yaml` 并未落地**，其结构（agent/budget/routing/verification/
> observability 等）为历史蓝图，勿照抄。键位与默认值以下方 TOML 为准。

```toml
# —— 模型路由 ——
model_provider = "deepseek"                 # 调用方未指定 reasoning_effort 时直接使用
effort_levels = ["low", "high", "max"]      # 合法等级，同时决定就近双向降级顺序

[purpose_effort]                            # purpose → reasoning_effort；未命中走 default
synthesize = "low"
direct_answer = "low"
analyze = "high"
plan = "high"
verify = "max"
grade = "high"
repair = "high"
rerank = "low"
default = "high"

[embedding]                                 # 语义向量源与语义重排；不可用时自动降级
enabled = true
model = "qwen3.7-text-embedding-flash"
base_url = "https://…/compatible-mode/v1"
key_env = "AI_KEY_QW"

[rerank]                                    # 专用 rerank 端点；不可用回退 embedding 余弦 → LLM 打分
enabled = false
model = "qwen3.7-text-rerank"
base_url = "https://…/text-rerank"
style = "dashscope"                         # dashscope | cohere
key_env = "AI_KEY_QW"

[retrieval]
hyde_enabled = false
hyde_max_tokens = 256
parent_child_enabled = false
parent_max_chars = 1800
max_context_tokens = 6000

[database]                                  # pgvector 持久化；连接失败回退内存
enabled = false
dsn_env = "ZCAISAGENT_DATABASE_URL"
dimensions = 1024

[model_providers.<id>]                      # 每个 provider 绑定一个 reasoning_effort 等级
name = "显示名"
model = "传给 /chat/completions 的模型名"
base_url = "不带尾部斜杠"
key_env = "AI_KEY_XX"
context_window = 800000
reasoning_effort = "high"                   # low | high | max
cost_in = 0.28                              # $/1M tokens（示例，按账单修改）
cost_out = 0.42

[security]
identity = "api_key"                        # api_key | oidc
roles = { support = ["retrieve","tool:read","run:create"], admin = ["retrieve","tool:read","tool:write","run:create","run:approve","admin:read","admin:write"] }
acl_tokens_by_role = { support = ["public"], admin = ["public","finance"] }
[security.oidc]
introspection_url = ""
client_id = ""
client_secret_env = "ZCAISAGENT_OIDC_CLIENT_SECRET"
timeout_s = 5.0
[security.opa]
url = ""                                    # 为空使用内置 RBAC
timeout_s = 3.0

[tools]
enable_kb_search = true
enable_ticket = false
mcp_servers = []
openapi = {}
sql = {}

[memory]
enabled = true
distill_enabled = true
max_items = 200
ttl_s = 0

[cache]                                     # 默认关闭；backend=redis 需可选依赖，失败回退 memory
enabled = false
ttl_s = 300
max_entries = 1000
llm_enabled = true
backend = "memory"
redis_url = "redis://localhost:6379/0"
redis_timeout_s = 1.0

[scheduler]                                 # 默认关闭；调度器只产生 Run
enabled = false
tick_s = 30

[events]                                    # Webhook 事件投递；默认关闭
enabled = false
webhook_urls = []
secret_env = "ZCAISAGENT_EVENT_SECRET"
max_retries = 3
dead_letter_path = ".zcaisagent/dead_letter.jsonl"

[secret]                                    # env | vault
provider = "env"
vault_addr = ""
vault_token_env = "VAULT_TOKEN"
vault_mount = "secret"
vault_path = "zcaisagent"

[studio]                                    # Studio 看板数据源
eval_report_path = "data/eval/latest.json"
eval_gate_path = "configs/eval_gate.toml"
baseline_dir = "data/eval/baselines"

[observability]                             # 细粒度事件默认开；OTel 默认关（需 .[otel]）
emit_events = true
otel_enabled = false
otel_endpoint = ""
service_name = "zcaisagent"
sample_ratio = 1.0

[guardrail]                                 # 规则之外可选 LLM 分类；失败 fail_open
model_enabled = false
purpose = "guardrail"
fail_open = true

[evaluator]                                 # 语义 NLI judge；默认关，失败回退词面
semantic_verify_enabled = false
purpose = "verify"
min_entailment = 0.8
fail_open = true

[skills]                                    # 启动扫描 skills/*/SKILL.md
enabled = true
dir = "skills"

[checkpoint]                                # jsonl（压缩+索引）| postgres（需 [database]）
backend = "jsonl"
compact_enabled = true
compact_threshold_lines = 20
compact_keep = 1
```

**预算口径**：运行时预算档位由 `orchestration/policies/budget.py` 定义。**节点步数**按节点执行次数计：
`L0 max_steps=4 / L1=16 / L2=22 / L3=32`（墙钟 10/60/40/120s，token 4000/20000/60000/180000）。
**有效动作次数**（retrieve/tool_call 等，每执行一次计 1）另由 `Budget.max_actions` 作为**运行时硬上限**：
`L0/L1/L2/L3 = 1/3/8/15`，与节点步数并行强制；PRD 的「平均有效动作 ≤2/5/12」是评测目标均值
（详见 PRD FR-LOOP-05 / FR-AI-08）。

**配置校验**：未知/缺失键按数据类默认值兜底；一个 provider 都未配置时回退 `FakeLLM`，无需 API Key
即可跑通。

---

## 18. 扩展接口矩阵与预留契约（MCP / Skill / Cron / Security）

> **2026-11-11 实现现状（§18.2–§18.6）**：
> - **Skill（§18.2）**：实现为 `domain.Skill` 扁平模型 + `skills/*/SKILL.md` 目录加载（零依赖 frontmatter 解析，支持 `when_to_use/tags/allowed_*/required_scopes/min_citation_support`），`match` 支持关键词 + tags；未做 `SkillMeta/SkillSpec/SkillMatch` 嵌套（保持轻量）。
> - **Cron（§18.3）**：`SchedulerService` 为**进程内内存实现**（`job_id:scheduled_at` 幂等、forbid/replace/allow、skip/run_once 补跑），**未实现** Redis `SETNX` 分布式互斥；单 worker 部署（见 06 §4.1）。
> - **Guardrail（§18.5）**：`RuleGuardrail` 规则优先；`guardrail.model_enabled=true` 时叠加可选 `LayeredGuardrail` LLM 分类（默认关、失败按 `fail_open` 回退规则）。
> - **MCP Server / A2A / Parser（§18.4/§18.6）**：端口为预留，默认未实现；`EventSinkPort.publish` 参数实为 `EventEnvelope`（非 `RunEvent`）。

> 回答一个关键问题：**这些能力现在是"有架构位置但没接口"，还是"连位置都没有"？**
> 结论：MCP 与 Security 已有位置但接口未完全显式化；Skill 与 Cron 原先只有产品层提及，**没有运行时契约**。本章统一补齐，且**不阻塞 M0 的 7 个核心端口**——按里程碑增量引入。

### 18.1 接口总览

| 能力 | 端口/契约 | 现状 | 默认实现 | 引入里程碑 |
|---|---|---|---|---|
| **MCP 接入（Client）** | 复用 `ToolPort` + `ToolSpec` | ✅ 已预留 | `capabilities/tools/mcp_client.py` | M1 |
| **MCP 对外（Server）** | `McpServerPort` | 🆕 本章新增 | `adapters/mcp/server.py` | V1 |
| **Skill（技能包）** | `SkillRegistryPort` | 🆕 本章新增 | `capabilities/skills/` | M1 |
| **Cron / 定时任务** | `SchedulerPort` + `JobSpec` | 🆕 本章新增 | `scheduler/` | M2 |
| **认证 AuthN** | `IdentityPort` | 🆕 本章新增 | OIDC / API Key | M0（API Key） |
| **授权 AuthZ** | `PolicyPort` | 🆕 本章新增 | 内置 RBAC；可选 OPA | M0（RBAC） |
| **护栏 Guardrails** | `GuardrailPort` | 🆕 本章新增 | 规则 + 模型双检 | M2 |
| **审计** | `AuditPort` | 🆕 本章新增 | Postgres append-only | M0（基础）/M2（完整） |
| **密钥** | `SecretPort` | 🆕 本章新增 | env / Vault / KMS | M0（env） |
| **事件订阅 / Webhook** | `EventSinkPort` | 🆕 本章新增 | HTTP + 签名重试 | M2 |
| **Agent 互操作（A2A）** | `AgentPeerPort` | 🆕 本章新增 | A2A 适配器 | V2 |
| **多模态解析** | `ParserPort` | 🆕 本章新增 | PDF/OCR/表格 | V2 |
| 检索源 / 模型 / 存储 / 编排引擎 | `RetrieverPort` / `LLMPort` / `CheckpointPort` / `RuntimePort` | ✅ 已预留 | 见 §5、§3 | M0 |

**设计约束**：新增端口一律遵循 §5.1（签名稳定、不泄漏实现类型、必传 `AuthContext`、可单测、错误归一化）。M0 实现 7 个核心端口 + 最小安全端口（Identity/Policy/Audit/Secret）；其余端口以 **fake/Noop 实现**占位，保证接口先冻结、实现后补。

### 18.2 Skill：能力层的"可复用配置包"

**定位**：Skill **不是**新的运行时、不是独立 Agent，而是"**指令 + 工具白名单 + 知识资源 + 策略覆盖**"的组合单元。由 Orchestrator 在路由阶段匹配、按需加载，复用同一套状态机、预算与追踪。

```python
# ports/skills.py
class SkillMeta(BaseModel):
    name: str
    version: str
    description: str                 # 常驻，用于匹配
    when_to_use: str                 # 触发条件，给路由器看
    tags: list[str] = []

class SkillSpec(BaseModel):
    meta: SkillMeta
    instructions: str                # SKILL.md 正文，按需加载（渐进披露）
    allowed_tools: list[str] = []    # 工具白名单（空 = 不允许调用工具）
    allowed_sources: list[str] = []  # 可用检索源
    required_scopes: list[str] = []  # 所需权限，必须是 ctx.scopes 的子集
    policy_overrides: dict[str, Any] = {}   # 只能收紧，不能放宽
    input_schema: dict | None = None
    output_schema: dict | None = None
    max_context_tokens: int | None = None
    schema_version: int = 1

class SkillMatch(BaseModel):
    name: str
    version: str
    score: float
    reason: str

class SkillRegistryPort(Protocol):
    async def list(self, ctx: "AuthContext") -> list[SkillMeta]: ...
    async def match(self, query: str, ctx: "AuthContext", top_k: int = 3) -> list[SkillMatch]: ...
    async def load(self, name: str, version: str, ctx: "AuthContext") -> SkillSpec: ...
```

**生命周期**：

```mermaid
flowchart LR
    A["启动扫描<br/>skills/*/SKILL.md"] --> B["注册 SkillMeta<br/>（元数据常驻）"]
    B --> C["路由匹配<br/>向量 + 规则 + 标签"]
    C --> D{"权限校验<br/>required_scopes ⊆ ctx.scopes"}
    D -->|"否"| X["拒绝加载<br/>fallback 通用策略"]
    D -->|"是"| E["按需加载正文<br/>+ 工具白名单 + 策略覆盖"]
    E --> F["Orchestrator 执行<br/>复用同一 RunState/Budget"]
    F --> G["审计 skill.loaded / executed"]
```

| 规则 | 说明 |
|---|---|
| **渐进披露** | 元数据（name/description/when_to_use）常驻上下文；正文只在命中后加载 |
| **不可提权** | `required_scopes` 必须 ⊆ 当前用户 scope；`policy_overrides` 只能降低预算/节点步数，不能提高 |
| **不自动执行脚本** | Skill 目录内的脚本必须经 `ToolGateway` + 沙箱执行；禁止 `subprocess` 直调 |
| **来源可信** | 内置 Skill 随版本发布；第三方 Skill 需签名 + 管理员审核 + 租户白名单 |
| **失败隔离** | Skill 加载/执行失败 → 回退通用策略，不中断整个 Run |
| **版本并存** | `name + version` 唯一；灰度按租户/比例切换；旧版本保留一个 minor |

**目录约定**（内容资产与代码分离）：

```
skills/
└── policy_compare/
    ├── SKILL.md          # frontmatter: name/version/description/when_to_use/scopes
    ├── references/       # 参考资料（可选，可被检索）
    └── scripts/          # 可选脚本（仅通过沙箱工具执行）
```

### 18.3 Cron：只做触发器，不复制执行路径

**定位**：定时任务**不是**第二套运行逻辑。调度器只负责"按时触发"，实际执行复用 `POST /v1/runs` 与同一个 GraphRuntime。

```python
# ports/scheduler.py
class NotifyTarget(BaseModel):
    kind: Literal["inbox", "webhook", "email"]
    target: str

class JobSpec(BaseModel):
    job_id: str
    tenant_id: str
    name: str
    cron: str                              # 5 段或 6 段（含秒）；如 "0 8 * * 1-5"
    timezone: str = "Asia/Shanghai"
    agent: str                             # 复用的 agent 配置名
    input: dict                            # goal / 参数 / 数据源范围
    identity: str                          # 服务账号（最小 scope）
    enabled: bool = True
    concurrency: Literal["forbid", "replace", "allow"] = "forbid"
    misfire: Literal["skip", "run_once"] = "run_once"
    timeout_s: int = 300
    budget_profile: str = "scheduled"      # 独立预算档，避免挤占在线
    notify: list[NotifyTarget] = []
    schema_version: int = 1

class JobRun(BaseModel):
    job_id: str
    run_id: str
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status: Literal["queued", "running", "succeeded", "failed", "skipped"]
    error: str | None = None

class SchedulerPort(Protocol):
    async def upsert(self, job: JobSpec) -> str: ...
    async def pause(self, job_id: str) -> None: ...
    async def resume(self, job_id: str) -> None: ...
    async def delete(self, job_id: str) -> None: ...
    async def list_runs(self, job_id: str, limit: int = 20) -> list[JobRun]: ...
```

| 语义 | 规则 |
|---|---|
| **幂等** | `idempotency_key = f"{job_id}:{scheduled_at}"`；重复触发只执行一次 |
| **分布式互斥** | Redis `SETNX`（key=job_id）防多副本重复触发；TTL = timeout + 余量 |
| **并发策略** | `forbid`（上一次未结束则跳过）/ `replace`（取消旧任务）/ `allow`（并行） |
| **错过执行** | `skip` 或 `run_once`（补跑一次，不补全部历史） |
| **身份** | 以服务账号执行，scope 最小化；结果投递需目标（收件人/频道）权限 |
| **预算** | 独立 `budget_profile`，与在线请求隔离，防定时任务打爆成本 |
| **审计** | `job.created/triggered/succeeded/failed/skipped` 全量记录 |
| **可观测** | `JobRun.run_id` 关联完整 trace；失败告警走通知通道 |

**禁止**：在 scheduler 里直接写业务逻辑、直接调 LLM、绕过预算与权限。调度器只产生 Run。

### 18.4 MCP：双向预留

| 方向 | 职责 | 契约 | 关键点 |
|---|---|---|---|
| **Client（接入外部）** | 把外部 MCP Server 的工具/资源暴露给 Agent | 复用 `ToolPort`；`mcp_client.py` 负责协议与 schema 映射 | stdio / SSE / Streamable HTTP；OAuth；工具列表缓存；超时与熔断；输出 sanitize |
| **Server（对外暴露）** | 把本系统能力暴露给其他 Agent/IDE | `McpServerPort` | 默认关闭；显式开启；租户绑定；scope 映射；限流；审计 |

```python
# ports/mcp_server.py
class McpServerPort(Protocol):
    async def start(self, config: "McpServerConfig") -> None: ...
    async def stop(self) -> None: ...
    def list_capabilities(self) -> list["McpCapability"]: ...
```

建议对外暴露的三类能力（按需开启）：

| MCP Tool | 入参 | 出参 | 风险 |
|---|---|---|---|
| `rag.search` | query, sources, top_k | `Evidence[]` | 只读 |
| `agent.run` | goal, level, budget_profile | `Answer` + citations + trace_id | 只读（可能触发工具） |
| `knowledge.ingest` | path/url, acl | job_id | 写，需审批 |

**安全**：MCP Server 不绕过 `IdentityPort`/`PolicyPort`；每个 tool 调用都要 `AuthContext`；跨租户默认拒绝；输出不得包含其他租户数据。

### 18.5 Security：五个显式端口

```python
# ports/security.py
class Decision(BaseModel):
    allowed: bool
    reason: str
    obligations: dict = {}          # 如 "mask_pii" / "require_approval"

class IdentityPort(Protocol):
    async def authenticate(self, token: str, request_meta: dict) -> "AuthContext": ...

class PolicyPort(Protocol):
    async def authorize(self, action: str, resource: "Resource", ctx: "AuthContext") -> Decision: ...

class GuardrailResult(BaseModel):
    allowed: bool
    category: str | None = None     # injection / pii / toxic / secret
    sanitized_text: str | None = None
    details: dict = {}

class GuardrailPort(Protocol):
    async def check_input(self, text: str, ctx: "AuthContext") -> GuardrailResult: ...
    async def check_output(self, text: str, ctx: "AuthContext") -> GuardrailResult: ...
    async def check_evidence(self, evidence: "Evidence", ctx: "AuthContext") -> GuardrailResult: ...

class AuditPort(Protocol):
    async def append(self, event: "AuditEvent") -> None: ...

class SecretPort(Protocol):
    async def get(self, key: str) -> str: ...
```

| 端口 | 内置实现 | 可替换 | 接入点 |
|---|---|---|---|
| `IdentityPort` | API Key / OIDC | SAML、LDAP、企业 SSO | API 中间件 |
| `PolicyPort` | RBAC + 租户 + ACL | OPA / Casbin / 自研策略中心 | 检索前、工具调用前、MCP Server |
| `GuardrailPort` | 规则（正则/词典）+ 小模型 | Llama Guard、云内容安全 | 输入、证据入上下文前、输出前 |
| `AuditPort` | Postgres append-only | Kafka、SIEM | 所有敏感动作 |
| `SecretPort` | 环境变量 | Vault、KMS、云密钥管理 | 启动与调用时 |

**安全默认值**：`deny by default`；工具默认只读；MCP Server 默认关闭；跨租户默认拒绝；审计失败不阻断主流程但告警（高敏动作可配置为阻断）。

### 18.6 事件订阅与 A2A（V2 预留）

```python
class EventSinkPort(Protocol):
    async def publish(self, event: "EventEnvelope") -> None: ...

class AgentPeerPort(Protocol):
    async def discover(self, capability: str) -> list["AgentCard"]: ...
    async def send(self, peer_id: str, task: "AgentTask", ctx: "AuthContext") -> "AgentTaskResult": ...
```

- **Webhook**：订阅 `run.finished` / `approval.requested` / `job.failed`；HMAC 签名；指数退避重试；死信队列。
- **A2A**：仅在多 Agent 协作场景启用；AgentCard 声明能力与权限；跨 Agent 调用必须携带 `AuthContext` 与预算；默认关闭。

### 18.7 版本与兼容策略

| 对象 | 版本字段 | 兼容规则 |
|---|---|---|
| 端口 | `v1` 命名空间 | 只增不改；破坏性变更新建 v2，双实现并存 ≥ 1 个 minor |
| `SkillSpec` / `JobSpec` | `schema_version` | 读取时向后兼容；写入总是最新版；迁移脚本 |
| 事件 | `event_version` | 消费者按版本解析；新增字段可选 |
| MCP | 协议版本协商 | 不支持则降级为 ToolPort 直连 |
| 配置 | `schema_version` | 启动校验；不兼容直接拒绝启动（fail fast） |

### 18.8 本章与里程碑的对应

| 里程碑 | 必须落地 | 可用 Noop/Fake 占位 |
|---|---|---|
| **M0** | `IdentityPort`(API Key)、`PolicyPort`(RBAC 骨架)、`AuditPort`(基础)、`SecretPort`(env) | Skill、Cron、MCP Server、EventSink、A2A |
| **M1** | `SkillRegistryPort` + 1 个内置 Skill、MCP Client 接 1 个工具 | Cron、MCP Server、Guardrail 完整版 |
| **M2** | `SchedulerPort` + 订阅调研任务、`GuardrailPort`、`AuditPort` 完整、`EventSinkPort`(Webhook) | A2A、多模态 |
| **V1/V2** | `McpServerPort`、`AgentPeerPort`、`ParserPort` 多模态 | — |

> 这样既回答"有没有预留"，也避免"为了预留而过度实现"。**接口先冻结，实现按需补**，是本架构对扩展性的核心承诺。

---

## 19. 借鉴 dsh-superagent 的机制

参考 `deepseek-ai/dsh-superagent-prototype`（MIT，研究原型）的领域模型与运行时设计，本架构吸收以下 4 项，并已落到 M0 代码：

| dsh 机制 | 核心思想 | zCAISAgent 落地 | 状态 |
|---|---|---|---|
| **SourceKind / SourceRegistry** | 来源是可注册的能力，插件化扩展；注册时校验命名与健康项 | `capabilities/retrieval/registry.py` + `RetrieverPoolPort`；多源并行、单源失败降级 | ✅ M0 |
| **Mandate（授权）** | 授权与上下文分离：记录"谁、依据哪版策略、可做什么"，上下文不授予权限 | `domain/mandate.py` + `RunState.mandate`；由 `AuthContext` 生成，记录 `policy_version/strength/scopes/acl` | ✅ M0 |
| **派生阶段表（Ticket stage）** | 不重复存状态，由事实推导"当前在等谁"；面板与告警共用同一张表 | `domain/stage.py::ticket_stage()`；API 返回 `stage`（queued/working/person review/blocked…） | ✅ M0 |
| **HealthBoard（健康板）** | 每个部件一个健康项、一个写入者；失败连续出现才报错 | `ports/health.py` + `adapters/health/memory.py`；`/health` 返回组件级状态，Runtime 失败时置 down | ✅ M0 |

### 19.1 尚未吸收、但已列入路线图的机制

| 机制 | 价值 | 计划 |
|---|---|---|
| **Effects / Outbox 持久化副作用队列** | 命令内入队、提交后执行；重试/退避/合并/按会话保序；崩溃不丢外部动作 | M1 接入 `audit`、`webhook`、`notify` |
| **Trigger / Thread / Subscription 模型** | 事件幂等、线程级串行 Eval、上下文扇出；避免重复处理同一消息 | M1 引入 `Trigger`（幂等键）与 `Thread`（会话聚合） |
| **单一写入者 + 角色化 Engine 接口** | 每类状态只有一个写者；调用方只拿到自己角色的方法，防越权耦合 | M1 将 `Runtime` 拆为 `EngineForApi` / `EngineForSources` / `EngineForExecutor` |
| **Prompt / Skill 版本化** | 提示与技能按版本快照、可回滚、可审计 | 随 `SkillRegistryPort` 在 M1 落地 |

> **不照搬的部分**：dsh 的 Ticket/Assignment/Session 生命周期围绕 GitHub/Feishu 场景设计，zCAISAgent 不直接复制其表结构；只吸收"授权与上下文分离、派生状态、单一写入者、可插拔来源、健康板"这些与场景无关的原则。

---

**下一篇**：[04-engineering-plan.md](04-engineering-plan.md) — 目录骨架、扩展点、里程碑与 ADR
