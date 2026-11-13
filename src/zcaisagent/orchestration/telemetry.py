"""细粒度事件的统一发射入口。

- 受 ``observability.emit_events`` 控制（默认开），写入 trace（JSONL / OTel）；
- EventSink（Webhook）仍只投递高层生命周期事件（由上层 publish 决定），避免洪泛；
- 任何异常都被吞掉，绝不影响 Run 主流程。
"""
from __future__ import annotations

import time
from typing import Any

from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.models import RunState
from zcaisagent.ports.llm import LLMRequest, LLMResponse


def llm_call_record(
    request: LLMRequest,
    response: LLMResponse | None,
    started: float,
    *,
    text: str | None = None,
) -> dict[str, Any]:
    """整理一次 LLM 调用为轨迹数据：prompt 与原始响应原样保留，并带本次耗时。"""
    system = "\n\n".join(m.content for m in request.messages if m.role == "system")
    prompt = "\n\n".join(m.content for m in request.messages if m.role != "system")
    if text is not None:
        output = text
    else:
        output = response.text if response is not None else ""
    return {
        "purpose": request.purpose,
        "model": response.model if response is not None else "",
        "system": system,
        "prompt": prompt,
        "response": output,
        "tokens": (
            response.tokens_in + response.tokens_out if response is not None else 0
        ),
        "cost_usd": round(response.cost_usd, 6) if response is not None else 0.0,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


async def emit(
    ctx: Any,
    state: RunState,
    event_type: str,
    *,
    node: str | None = None,
    data: dict[str, Any] | None = None,
    span: str | None = None,
) -> None:
    try:
        config = getattr(ctx, "config", None)
        obs = getattr(config, "observability", None)
        if obs is not None and not getattr(obs, "emit_events", True):
            return
        trace = getattr(ctx, "trace", None)
        if trace is None:
            return
        payload = data or {}
        trace.event(
            RunEvent(
                type=event_type,
                run_id=state.run_id,
                step=state.step,
                node=node or state.last_node,
                span=span,
                data=payload,
            )
        )
    except Exception:  # noqa: BLE001 - telemetry 绝不影响主流程
        return
