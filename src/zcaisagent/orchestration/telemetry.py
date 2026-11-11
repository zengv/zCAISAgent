"""细粒度事件的统一发射入口。

- 受 ``observability.emit_events`` 控制（默认开），写入 trace（JSONL / OTel）；
- EventSink（Webhook）仍只投递高层生命周期事件（由上层 publish 决定），避免洪泛；
- 任何异常都被吞掉，绝不影响 Run 主流程。
"""
from __future__ import annotations

from typing import Any

from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.models import RunState


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
