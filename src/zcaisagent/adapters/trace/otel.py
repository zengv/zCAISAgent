"""可选 OTel trace 适配器：把节点步骤映射为 span，事件委托给底层 JSONL trace。

设计为可选：opentelemetry 未安装或初始化失败时抛 ``TraceUnavailable``，
由容器回退到纯 JSONL trace。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.models import RunState


class TraceUnavailable(Exception):
    """OTel SDK/导出器不可用；调用方应回退 JsonlTrace。"""


class OtelTrace:
    def __init__(
        self,
        delegate: Any,
        service_name: str = "zcaisagent",
        endpoint: str = "",
        sample_ratio: float = 1.0,
        exporter: Any = None,
    ) -> None:
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor,
                SimpleSpanProcessor,
            )
        except ImportError as exc:  # noqa: BLE001 - 可选依赖
            raise TraceUnavailable("opentelemetry-sdk not installed") from exc
        self.delegate = delegate
        try:
            provider = TracerProvider(
                resource=Resource.create({"service.name": service_name})
            )
            if exporter is not None:
                provider.add_span_processor(SimpleSpanProcessor(exporter))
            elif endpoint:
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )
                except ImportError as exc:  # noqa: BLE001 - 可选依赖
                    raise TraceUnavailable("otlp exporter not installed") from exc
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
                )
            self._provider = provider
            self._tracer = provider.get_tracer(service_name)
        except TraceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 初始化失败统一回退
            raise TraceUnavailable(str(exc)) from exc
        self._spans: dict[tuple[str, int], Any] = {}

    def start_step(self, step: int, node: str, run_id: str = "") -> None:
        self.delegate.start_step(step, node, run_id)
        try:
            self._spans[(run_id, step)] = self._tracer.start_span(name=f"step.{node}")
        except Exception:  # noqa: BLE001
            pass

    def end_step(self, state: RunState, details: dict[str, Any] | None = None) -> None:
        self.delegate.end_step(state, details)
        span = self._spans.pop((state.run_id, state.step), None)
        if span is None:
            return
        try:
            if state.last_node:
                span.set_attribute("node", state.last_node)
            span.set_attribute("status", state.status.value)
            span.end()
        except Exception:  # noqa: BLE001
            pass

    @contextmanager
    def span(self, name: str, **attrs: object):
        try:
            with self._tracer.start_as_current_span(
                name, attributes={key: str(value) for key, value in attrs.items()}
            ) as child:
                yield child
        except Exception:  # noqa: BLE001
            yield None

    def event(self, event: RunEvent) -> None:
        self.delegate.event(event)
        try:
            span = next(
                (s for (rid, _), s in self._spans.items() if rid == event.run_id), None
            )
            if span is not None:
                span.add_event(event.type, {k: str(v) for k, v in event.data.items()})
        except Exception:  # noqa: BLE001
            pass

    def events(self, run_id: str) -> list[RunEvent]:
        return self.delegate.events(run_id)

    def force_flush(self, timeout_millis: int = 30000) -> None:
        try:
            self._provider.force_flush(timeout_millis)
        except Exception:  # noqa: BLE001
            pass
