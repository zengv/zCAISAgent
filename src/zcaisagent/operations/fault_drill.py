"""故障演练：用注入的失败适配器与不可达端点验证四类降级路径。

场景：
1. 模型失败：主 provider 500 时按 fallback 链切换；全部失败时 Run 以 FAILED
   收尾且步数有限。
2. 检索源失败：单源抛异常时其余源证据保留，轨迹标记 degraded。
3. PG 不可用：容器回退内存检索 + 种子语料，健康板标记 database=down。
4. Redis 不可用：缓存回退进程内实现，读写不抛异常并标记降级。

演练报告默认写入 ``data/drills/fault-drill.json``（原子替换），任一场景失败
只影响该场景结果，脚本仍跑完其余场景。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from zcaisagent.adapters.checkpoint.jsonl import InMemoryCheckpoint
from zcaisagent.adapters.health.memory import HealthBoard
from zcaisagent.adapters.llm.fake import FakeLLM
from zcaisagent.adapters.llm.openai_compat import OpenAICompatLLM
from zcaisagent.adapters.trace.jsonl import JsonlTrace
from zcaisagent.capabilities.retrieval.registry import SourceRegistry
from zcaisagent.capabilities.retrieval.sources.inmemory import InMemoryRetriever
from zcaisagent.container import build_cache, build_container
from zcaisagent.domain.enums import AutonomyLevel, RunStatus, SourceType
from zcaisagent.domain.models import Chunk, Message, RunState
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.nodes.retrieve import retrieve_node
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from zcaisagent.orchestration.runtime import RunContext, Runtime
from zcaisagent.ports.health import HealthStatus
from zcaisagent.ports.llm import LLMDelta, LLMRequest
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuthContext
from zcaisagent.settings import CacheConfig, DatabaseConfig, ModelProvider, Settings

DEFAULT_REPORT_PATH = Path("data/drills/fault-drill.json")

_PRIMARY_HOST = "p1.drill.test"
_BACKUP_HOST = "p2.drill.test"
_PRIMARY_KEY = "sk-p1-drill-secret"
_BACKUP_KEY = "sk-p2-drill-secret"
_DRILL_GOAL = "加班可以申请调休吗？"
_RUN_TIMEOUT_S = 20.0


def _provider(provider_id: str, host: str, model: str, api_key: str) -> ModelProvider:
    return ModelProvider(
        id=provider_id,
        name=provider_id,
        model=model,
        base_url=f"https://{host}/v1",
        api_key=api_key,
        context_window=8_000,
        reasoning_effort="low",
    )


def _drill_settings() -> Settings:
    """两个同等级 provider：默认 p1，失败后按 config 顺序降级到 p2。"""
    return Settings(
        model_provider="p1",
        model_providers={
            "p1": _provider("p1", _PRIMARY_HOST, "primary-model", _PRIMARY_KEY),
            "p2": _provider("p2", _BACKUP_HOST, "backup-model", _BACKUP_KEY),
        },
        purpose_effort={"default": "low"},
    )


def _completion(model: str, text: str) -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def _fallback_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls.append(host)
        if host == _PRIMARY_HOST:
            return httpx.Response(500, json={"error": "primary down"})
        return httpx.Response(200, json=_completion("backup-model", "backup ok"))

    return httpx.MockTransport(handler)


def _failing_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "all providers down"})

    return httpx.MockTransport(handler)


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message(role="user", content="ping")], max_tokens=16)


class _ExplodingLLM:
    """所有调用直接抛异常，用于验证 Runtime 把节点异常收敛为 FAILED。"""

    async def complete(self, req: LLMRequest):
        raise RuntimeError("simulated model outage")

    async def stream(self, req: LLMRequest):
        raise RuntimeError("simulated model outage")
        yield LLMDelta(text="")


class _FailingRetriever:
    source_name = "failing"
    source_type = SourceType.DOCUMENT

    async def retrieve(
        self, req: RetrieveRequest, ctx: AuthContext
    ) -> RetrieveResult:
        raise TimeoutError("simulated retrieval timeout")


def _drill_chunk() -> Chunk:
    return Chunk(
        id="drill#0",
        doc_id="drill",
        title="考勤与加班管理办法",
        content="加班可以申请调休。调休需在加班发生后六个月内使用。",
        index=0,
        tenant_id="demo",
        acl=["public"],
    )


def _auth() -> AuthContext:
    return AuthContext(
        tenant_id="demo",
        user_id="drill",
        roles=["admin"],
        scopes=["retrieve"],
        acl_tokens=["public"],
    )


def _make_state(goal: str, level: AutonomyLevel) -> RunState:
    return RunState(
        run_id="run_drill",
        thread_id="thread_drill",
        tenant_id="demo",
        user_id="drill",
        goal=goal,
        level=level,
        requested_level=level,
        messages=[Message(role="user", content=goal)],
        budget=BudgetPolicy.initial(level),
    )


async def _run_with_exploding_llm(tmp_path: Path) -> tuple[RunStatus, int, dict[str, str]]:
    """异常 LLM 触发 L2 Run：Runtime 捕获节点异常，不以挂起收尾。"""
    retriever = InMemoryRetriever()
    retriever.add([_drill_chunk()])
    health = HealthBoard()
    health.register("runtime", "runtime")
    health.report("runtime", HealthStatus.OK)
    ctx = RunContext(
        llm=_ExplodingLLM(),
        retriever=retriever,
        checkpoint=InMemoryCheckpoint(),
        trace=JsonlTrace(tmp_path / "drill-trace.jsonl"),
        auth=_auth(),
        config=Settings(top_k=5, data_dir=tmp_path),
        health=health,
        node_timeout_s=5.0,
    )
    state = _make_state(_DRILL_GOAL, AutonomyLevel.L2_STANDARD)
    result = await asyncio.wait_for(
        Runtime().execute(build_default_graph(), state, ctx), timeout=_RUN_TIMEOUT_S
    )
    snapshot = {entry.name: entry.status.value for entry in health.snapshot()}
    return result.status, result.step, snapshot


async def _scenario_model_failure(tmp_path: Path) -> tuple[str, dict[str, str]]:
    settings = _drill_settings()
    calls: list[str] = []
    llm = OpenAICompatLLM(settings=settings, transport=_fallback_transport(calls))
    response = await asyncio.wait_for(llm.complete(_request()), timeout=_RUN_TIMEOUT_S)
    assert calls == [_PRIMARY_HOST, _BACKUP_HOST], f"unexpected provider order: {calls}"
    assert response.model == "backup-model", f"unexpected model: {response.model}"
    assert response.text == "backup ok", f"unexpected text: {response.text}"

    failing = OpenAICompatLLM(settings=settings, transport=_failing_transport())
    try:
        await asyncio.wait_for(failing.complete(_request()), timeout=_RUN_TIMEOUT_S)
    except httpx.HTTPError as exc:
        http_error = type(exc).__name__
    else:
        raise AssertionError("all providers down should raise httpx.HTTPError")

    status, steps, health = await _run_with_exploding_llm(tmp_path)
    assert status == RunStatus.FAILED, f"run should fail, got {status}"
    assert steps < BudgetPolicy.initial(AutonomyLevel.L2_STANDARD).max_steps, (
        f"run consumed too many steps: {steps}"
    )
    detail = (
        f"p1 500 → p2 200 (model=backup-model); 全部失败 → {http_error}; "
        f"异常 LLM run={status.value} steps={steps}"
    )
    return detail, health


async def _scenario_retrieval_failure(
    tmp_path: Path,
) -> tuple[str, dict[str, str]]:
    normal = InMemoryRetriever()
    normal.add([_drill_chunk()])
    registry = SourceRegistry()
    registry.register(normal, priority=100)
    registry.register(_FailingRetriever(), priority=110)
    ctx = RunContext(
        llm=FakeLLM(),
        retriever=normal,
        checkpoint=InMemoryCheckpoint(),
        trace=JsonlTrace(tmp_path / "retrieval-trace.jsonl"),
        auth=_auth(),
        config=Settings(top_k=5, data_dir=tmp_path),
        sources=registry,
        node_timeout_s=5.0,
    )
    state = _make_state(_DRILL_GOAL, AutonomyLevel.L1_FAST)
    patch = await asyncio.wait_for(
        retrieve_node(state, ctx), timeout=_RUN_TIMEOUT_S
    )
    evidence = patch.append_evidence or []
    notes = patch.append_notes or []
    assert evidence, "healthy source should still contribute evidence"
    assert any(note.startswith("retrieve_error:failing") for note in notes), notes
    assert any(note.startswith("retrieve_degraded:failing") for note in notes), notes
    detail = f"failing 源超时；保留 inmemory 证据 {len(evidence)} 条，notes 含 degraded"
    return detail, {"inmemory": "ok", "failing": "down"}


async def _scenario_pg_unavailable(tmp_path: Path) -> tuple[str, dict[str, str]]:
    settings = Settings(
        database=DatabaseConfig(
            enabled=True,
            dsn="postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
            dimensions=8,
        ),
        data_dir=tmp_path,
        seed_dir=Path("data/seed"),
    )
    container = build_container(settings)
    snapshot = {entry.name: entry.status.value for entry in container.health.snapshot()}
    assert container.retriever.chunks, "seed chunks should be loaded into memory"
    assert snapshot.get("database") == HealthStatus.DOWN.value, snapshot
    detail = (
        f"PG 不可达 → 回退内存；seed_chunks={len(container.retriever.chunks)}; "
        f"health.database={snapshot.get('database')}"
    )
    return detail, {"database": snapshot.get("database", "unknown")}


async def _scenario_redis_unavailable(_tmp_path: Path) -> tuple[str, dict[str, str]]:
    settings = Settings(
        cache=CacheConfig(
            enabled=True,
            backend="redis",
            redis_url="redis://127.0.0.1:1/0",
            redis_timeout_s=0.2,
        )
    )
    cache = build_cache(settings)
    assert cache is not None, "cache should fall back to in-memory instead of None"
    first = await asyncio.wait_for(cache.get("k"), timeout=_RUN_TIMEOUT_S)
    assert first is None
    await asyncio.wait_for(cache.set("k", {"v": 1}), timeout=_RUN_TIMEOUT_S)
    degraded = bool(getattr(cache, "is_degraded", False))
    assert degraded, "redis cache should mark itself degraded after failure"
    assert await asyncio.wait_for(cache.get("k"), timeout=_RUN_TIMEOUT_S) == {"v": 1}
    detail = "Redis 不可达 → 回退 InMemoryCache；get/set 不抛异常，is_degraded=True"
    return detail, {"cache": "down" if degraded else "ok"}


ScenarioRunner = Callable[[Path], Awaitable[tuple[str, dict[str, str]]]]

_SCENARIOS: tuple[tuple[str, str, str, ScenarioRunner], ...] = (
    (
        "model_failure",
        "主 provider 返回 500 / 全部 provider 500 / LLM 直接抛异常",
        "OpenAICompatLLM fallback 链 p1→p2；全部失败抛 httpx.HTTPError；"
        "Runtime NODE_FAILED → FAILED",
        _scenario_model_failure,
    ),
    (
        "retrieval_failure",
        "FailingRetriever 抛 TimeoutError，InMemoryRetriever 正常",
        "retrieve_node 记录 retrieve_error/retrieve_degraded，保留其余源证据",
        _scenario_retrieval_failure,
    ),
    (
        "pg_unavailable",
        "Postgres DSN 指向不可达的 127.0.0.1:1",
        "build_container 回退内存检索 + 种子语料，health.database=down",
        _scenario_pg_unavailable,
    ),
    (
        "redis_unavailable",
        "Redis URL 指向不可达的 127.0.0.1:1",
        "build_cache 返回 RedisCache + InMemoryCache 回退，is_degraded=True",
        _scenario_redis_unavailable,
    ),
)


def _write_report(path: Path, report: dict) -> None:
    """原子写入：先写同目录临时文件，再整体替换，避免半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp, path)


async def run_drills(out_path: Path | None = None) -> dict:
    """执行四个故障场景并落盘报告；单场景失败不影响其余场景。"""
    path = Path(out_path) if out_path is not None else DEFAULT_REPORT_PATH
    scenarios: list[dict] = []
    for name, trigger, degraded_path, runner in _SCENARIOS:
        try:
            # 每个场景独立临时目录，trace/checkpoint 等中间产物不落到报告目录。
            with tempfile.TemporaryDirectory(prefix="zcaisagent-drill-") as tmp:
                detail, health = await runner(Path(tmp))
        except Exception as exc:  # noqa: BLE001 - 演练需跑完其余场景
            scenarios.append(
                {
                    "name": name,
                    "passed": False,
                    "trigger": trigger,
                    "degraded_path": degraded_path,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "health": {},
                }
            )
            continue
        scenarios.append(
            {
                "name": name,
                "passed": True,
                "trigger": trigger,
                "degraded_path": degraded_path,
                "detail": detail,
                "health": health,
            }
        )

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "passed": all(item["passed"] for item in scenarios),
        "scenarios": scenarios,
    }
    _write_report(path, report)
    return report
