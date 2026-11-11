from pathlib import Path

from zcaisagent.domain.models import RunState


class InMemoryCheckpoint:
    def __init__(self) -> None:
        self._states: dict[str, list[RunState]] = {}

    async def save(self, state: RunState) -> None:
        self._states.setdefault(state.run_id, []).append(state)

    async def load(self, run_id: str) -> RunState | None:
        history = self._states.get(run_id)
        return history[-1] if history else None

    async def list_thread(self, thread_id: str) -> list[RunState]:
        return [
            history[-1]
            for history in self._states.values()
            if history and history[-1].thread_id == thread_id
        ]

    async def list_runs(self, limit: int = 50) -> list[RunState]:
        states = [history[-1] for history in self._states.values() if history]
        states.sort(key=lambda state: (-state.created_ts, state.run_id))
        return states[: max(0, limit)]


class JsonlCheckpoint:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.jsonl"

    async def save(self, state: RunState) -> None:
        with self._path(state.run_id).open("a", encoding="utf-8") as handle:
            handle.write(state.model_dump_json() + "\n")

    async def load(self, run_id: str) -> RunState | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        last = path.read_text(encoding="utf-8").strip().splitlines()[-1]
        return RunState.model_validate_json(last)

    async def list_thread(self, thread_id: str) -> list[RunState]:
        result: list[RunState] = []
        for path in self.root.glob("*.jsonl"):
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            if not lines:
                continue
            state = RunState.model_validate_json(lines[-1])
            if state.thread_id == thread_id:
                result.append(state)
        return result

    async def list_runs(self, limit: int = 50) -> list[RunState]:
        states: list[RunState] = []
        for path in self.root.glob("*.jsonl"):
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            if not lines:
                continue
            states.append(RunState.model_validate_json(lines[-1]))
        states.sort(key=lambda state: (-state.created_ts, state.run_id))
        return states[: max(0, limit)]
