import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.models import RunState


class JsonlTrace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: dict[str, list[RunEvent]] = {}
        self._starts: dict[str, tuple[int, float]] = {}

    def start_step(self, step: int, node: str, run_id: str = "") -> None:
        self._starts[run_id] = (step, time.perf_counter())

    def end_step(self, state: RunState, details: dict[str, Any] | None = None) -> None:
        details = details or {}
        started = self._starts.pop(state.run_id, None)
        duration_ms = (
            round((time.perf_counter() - started[1]) * 1000, 1) if started is not None else None
        )
        data: dict[str, Any] = {
            "status": state.status.value,
            "duration_ms": duration_ms,
            "model": details.get("model"),
            "tokens": details.get("tokens", 0),
            "cost_usd": details.get("cost_usd", 0.0),
            "input": details.get("input", {}),
            "output": details.get("output", {}),
        }
        if details.get("node"):
            data["node"] = details["node"]
        self.event(
            RunEvent(
                type="step.finished",
                run_id=state.run_id,
                step=state.step,
                node=details.get("node") or state.last_node,
                data=data,
            )
        )

    @contextmanager
    def span(self, name: str, **attrs: object):
        yield None

    def event(self, event: RunEvent) -> None:
        self._events.setdefault(event.run_id, []).append(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")

    def events(self, run_id: str) -> list[RunEvent]:
        return list(self._events.get(run_id, []))
