"""Admin 平台 API：数据源、轨迹、指标、配置与评估。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from zcaisagent.api.admin_auth import require_admin_read, require_admin_write
from zcaisagent.api.deps import get_container
from zcaisagent.capabilities.retrieval.registry import SourceRegistration
from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.models import RunState
from zcaisagent.domain.stage import ticket_stage
from zcaisagent.evaluation.gate import check, load_thresholds
from zcaisagent.evaluation.observability import (
    build_metrics,
    quality_snapshot,
)
from zcaisagent.evaluation.report import build_report, resolve_report_path, write_report
from zcaisagent.evaluation.runner import filter_cases, load_cases, run_dataset
from zcaisagent.ports.security import AuditEvent, AuthContext
from zcaisagent.settings import Settings, StudioConfig

router = APIRouter()

AdminRead = Annotated[AuthContext, Depends(require_admin_read)]
AdminWrite = Annotated[AuthContext, Depends(require_admin_write)]

# 评估数据集的默认路径；看板报告路径统一来自 settings.studio。
_EVAL_CASES_PATH = Path("data/eval/golden.jsonl")

# 指标聚合最多回看的 Run 数，避免大目录全量扫描。
_METRICS_RUN_LIMIT = 1000


class SourceToggle(BaseModel):
    enabled: bool


class EvalRunRequest(BaseModel):
    tag: str | None = None
    limit: int = Field(default=10, ge=0, le=100)


def _cache_stats() -> dict[str, Any]:
    cache = get_container().cache
    if cache is None:
        return {"enabled": False, "hits": 0, "misses": 0, "hit_rate": 0.0}
    stats = cache.stats()
    return {
        "enabled": True,
        "hits": stats.hits,
        "misses": stats.misses,
        "hit_rate": stats.hit_rate,
    }


def _health_map() -> dict[str, str]:
    return {
        entry.name: entry.status.value
        for entry in get_container().health.snapshot()
    }


def _source_summary(
    registration: SourceRegistration, health: dict[str, str]
) -> dict[str, Any]:
    return {
        "name": registration.name,
        "type": registration.retriever.source_type.value,
        "priority": registration.priority,
        "enabled": registration.enabled,
        "health": health.get(registration.name, "unknown"),
    }


def _run_summary(
    state: RunState, duration_fallback_ms: float | None = None
) -> dict[str, Any]:
    answer = state.answer
    elapsed_ms = None
    if state.created_ts and state.finished_ts:
        elapsed_ms = max(0.0, (state.finished_ts - state.created_ts) * 1000)
    elif duration_fallback_ms is not None:
        # 旧 checkpoint 的 finished_ts 落盘为 0，用轨迹节点耗时之和兜底。
        elapsed_ms = duration_fallback_ms
    return {
        "run_id": state.run_id,
        "thread_id": state.thread_id,
        "tenant_id": state.tenant_id,
        "user_id": state.user_id,
        "goal": state.goal,
        "status": state.status.value,
        "stage": ticket_stage(state).value,
        "created_ts": state.created_ts,
        "finished_ts": state.finished_ts,
        "elapsed_ms": elapsed_ms,
        "steps": state.step,
        "tokens": state.budget.used_tokens,
        "cost": round(state.budget.used_cost_usd, 6),
        "answer_abstained": answer.abstained if answer else True,
    }


def _trace_durations() -> dict[str, float]:
    """从轨迹文件汇总每个 run 的节点耗时之和（ms），一次读取全量复用。"""
    path = get_container().settings.data_dir / "trace.jsonl"
    durations: dict[str, float] = {}
    if not path.exists():
        return durations
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = RunEvent.model_validate_json(line)
        except ValueError:
            continue
        if event.type != "step.finished":
            continue
        value = (event.data or {}).get("duration_ms")
        if isinstance(value, (int, float)):
            durations[event.run_id] = durations.get(event.run_id, 0.0) + value
    return durations


def _trace_events(run_id: str) -> list[RunEvent]:
    """优先读进程内轨迹，缺失时回退 data_dir/trace.jsonl。"""
    container = get_container()
    events = container.trace.events(run_id)
    if not events:
        path = container.settings.data_dir / "trace.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = RunEvent.model_validate_json(line)
                if event.run_id == run_id:
                    events.append(event)
    return events


def _source_names(raw: dict) -> list[str]:
    """兼容 ``{"sources": {name: {...}}}`` 与单源平铺两种写法。"""
    if not isinstance(raw, dict) or not raw:
        return []
    sources = raw.get("sources")
    if isinstance(sources, dict):
        return sorted(str(name) for name in sources)
    return ["default"]


def _config_snapshot(settings: Settings) -> dict[str, Any]:
    """脱敏配置快照：不含 api_key、DSN、Vault token 与签名密钥。"""
    security = settings.security
    tools = settings.tools
    memory = settings.memory
    cache = settings.cache
    scheduler = settings.scheduler
    events = settings.events
    database = settings.database
    return {
        "models": [
            {
                "id": provider.id,
                "name": provider.name,
                "model": provider.model,
                "base_url": provider.base_url,
                "effort": provider.reasoning_effort,
            }
            for provider in settings.model_providers.values()
        ],
        "purpose_effort": dict(settings.purpose_effort),
        "retrieval": {
            "hyde": settings.hyde_enabled,
            "parent_child": settings.parent_child_enabled,
            "top_k": settings.top_k,
            "max_context_tokens": settings.max_context_tokens,
        },
        "security": {
            "identity_mode": security.identity_mode if security else "api_key",
            "roles": dict(security.roles) if security else {},
            "acl_tokens_by_role": (
                dict(security.acl_tokens_by_role) if security else {}
            ),
        },
        "tools": {
            "enable_kb_search": bool(tools.enable_kb_search) if tools else False,
            "enable_ticket": bool(tools.enable_ticket) if tools else False,
            "mcp_servers": len(tools.mcp_servers) if tools else 0,
            "openapi_sources": _source_names(tools.openapi) if tools else [],
            "sql_sources": _source_names(tools.sql) if tools else [],
        },
        "memory": {
            "enabled": memory.enabled,
            "distill_enabled": memory.distill_enabled,
            "max_items": memory.max_items,
            "ttl_s": memory.ttl_s,
        },
        "cache": {
            "enabled": bool(cache.enabled) if cache else False,
            "ttl_s": cache.ttl_s if cache else 0.0,
            "max_entries": cache.max_entries if cache else 0,
            "llm_enabled": bool(cache.llm_enabled) if cache else False,
        },
        "scheduler": {
            "enabled": bool(scheduler.enabled) if scheduler else False,
            "tick_s": scheduler.tick_s if scheduler else 0.0,
        },
        "events": {
            "enabled": bool(events.enabled) if events else False,
            "webhook_count": len(events.webhook_urls) if events else 0,
            "max_retries": events.max_retries if events else 0,
            "dead_letter_path": str(events.dead_letter_path) if events else "",
        },
        "database": {
            "enabled": bool(database.enabled) if database else False,
            "dimensions": database.dimensions if database else 0,
        },
        "data_dir": str(settings.data_dir),
        "seed_dir": str(settings.seed_dir),
    }


def _studio() -> StudioConfig:
    return get_container().settings.studio


def _report_path() -> Path | None:
    studio = _studio()
    return resolve_report_path(studio.eval_report_path, studio.baseline_dir)


def _quality() -> dict[str, Any]:
    studio = _studio()
    return quality_snapshot(
        studio.eval_report_path,
        studio.eval_gate_path,
        baseline_dir=studio.baseline_dir,
    )


@router.get("/admin/cache/stats")
async def cache_stats(auth: AdminRead) -> dict[str, Any]:
    return _cache_stats()


@router.get("/admin/sources")
async def list_sources(auth: AdminRead) -> list[dict[str, Any]]:
    container = get_container()
    health = _health_map()
    return [
        _source_summary(registration, health)
        for registration in container.sources.registrations()
    ]


@router.put("/admin/sources/{name}")
async def toggle_source(
    name: str, body: SourceToggle, auth: AdminWrite
) -> dict[str, Any]:
    container = get_container()
    if not container.sources.set_enabled(name, body.enabled):
        raise HTTPException(status_code=404, detail="source not found")
    await container.audit.append(
        AuditEvent(
            action="admin.source_toggled",
            actor=auth.user_id,
            tenant_id=auth.tenant_id,
            detail={"source": name, "enabled": body.enabled},
        )
    )
    health = _health_map()
    registration = next(
        item
        for item in container.sources.registrations()
        if item.name == name
    )
    return _source_summary(registration, health)


@router.get("/admin/runs")
async def list_runs(
    auth: AdminRead,
    limit: int = 50,
    status: str | None = None,
) -> list[dict[str, Any]]:
    container = get_container()
    states = await container.checkpoint.list_runs(max(0, limit))
    items = [state for state in states if state.tenant_id == auth.tenant_id]
    if status:
        items = [state for state in items if state.status.value == status]
    durations = _trace_durations()
    return [
        _run_summary(state, durations.get(state.run_id)) for state in items
    ]


@router.get("/admin/runs/{run_id}/trace")
async def run_trace(run_id: str, auth: AdminRead) -> dict[str, Any]:
    container = get_container()
    state = await container.checkpoint.load(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    if state.tenant_id != auth.tenant_id:
        await container.audit.append(
            AuditEvent(
                action="admin.denied",
                actor=auth.user_id,
                tenant_id=auth.tenant_id,
                run_id=run_id,
                detail={
                    "action": "admin.read",
                    "reason": "cross_tenant_denied",
                    "path": f"/v1/admin/runs/{run_id}/trace",
                    "owner_tenant": state.tenant_id,
                },
            )
        )
        raise HTTPException(status_code=403, detail="run not accessible")
    events = _trace_events(run_id)
    return {
        "run": _run_summary(
            state,
            sum(
                (event.data or {}).get("duration_ms") or 0.0
                for event in events
                if event.type == "step.finished"
            ),
        ),
        "events": [event.model_dump() for event in events],
    }


@router.get("/admin/metrics")
async def metrics(auth: AdminRead, window: str = "24h") -> dict[str, Any]:
    container = get_container()
    states = [
        state
        for state in await container.checkpoint.list_runs(_METRICS_RUN_LIMIT)
        if state.tenant_id == auth.tenant_id
    ]
    try:
        return build_metrics(states, _cache_stats(), window, _quality())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid window") from None


@router.get("/admin/config")
async def config_snapshot(auth: AdminRead) -> dict[str, Any]:
    return _config_snapshot(get_container().settings)


@router.get("/admin/eval/latest")
async def eval_latest(auth: AdminRead) -> dict[str, Any]:
    if _report_path() is None:
        raise HTTPException(status_code=404, detail="eval report not found")
    snapshot = _quality()
    if not snapshot.get("available"):
        raise HTTPException(status_code=404, detail="eval report not found")
    return snapshot


@router.post("/admin/eval/run")
async def eval_run(
    auth: AdminWrite, body: EvalRunRequest | None = None
) -> dict[str, Any]:
    payload = body or EvalRunRequest()
    if not _EVAL_CASES_PATH.is_file():
        raise HTTPException(status_code=404, detail="eval dataset not found")
    cases = filter_cases(load_cases(_EVAL_CASES_PATH), tag=payload.tag)
    cases = cases[: payload.limit]
    raw = await run_dataset(get_container(), cases)
    report = build_report(raw, len(cases))
    studio = _studio()
    write_report(report, studio.eval_report_path)
    thresholds = load_thresholds(studio.eval_gate_path)
    result = check(report, thresholds)
    return {
        "report": report,
        "thresholds": asdict(thresholds),
        "gate": {"passed": result.passed, "violations": result.violations},
        "path": str(_EVAL_CASES_PATH),
        "report_path": str(studio.eval_report_path),
        "generated_at": report["generated_at"],
    }
