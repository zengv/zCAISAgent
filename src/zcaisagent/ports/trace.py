from contextlib import AbstractContextManager
from typing import Any, Protocol

from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.models import RunState


class TracePort(Protocol):
    def start_step(self, step: int, node: str, run_id: str = "") -> None: ...

    def end_step(self, state: RunState, details: dict[str, Any] | None = None) -> None: ...

    def span(self, name: str, **attrs: object) -> AbstractContextManager[None]: ...

    def event(self, event: RunEvent) -> None: ...

    def events(self, run_id: str) -> list[RunEvent]: ...
