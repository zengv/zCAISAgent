from typing import Protocol

from zcaisagent.domain.models import RunState


class CheckpointPort(Protocol):
    async def save(self, state: RunState) -> None: ...

    async def load(self, run_id: str) -> RunState | None: ...

    async def list_thread(self, thread_id: str) -> list[RunState]: ...
