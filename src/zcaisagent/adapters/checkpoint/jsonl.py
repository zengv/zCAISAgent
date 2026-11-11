import json
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
    """每个 Run 一个 append-only JSONL；超过阈值压缩为最新快照，并用索引加速列表。"""

    _INDEX_NAME = ".index.jsonl"

    def __init__(
        self,
        root: Path,
        compact_enabled: bool = True,
        compact_threshold_lines: int = 20,
        compact_keep: int = 1,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.compact_enabled = compact_enabled
        self.compact_threshold_lines = max(1, int(compact_threshold_lines))
        self.compact_keep = max(1, int(compact_keep))
        self._counts: dict[str, int] = {}
        self._index: dict[str, dict] = {}
        self._load_index()

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.jsonl"

    def _index_path(self) -> Path:
        return self.root / self._INDEX_NAME

    def _run_files(self) -> list[Path]:
        return [p for p in self.root.glob("*.jsonl") if p.name != self._INDEX_NAME]

    @staticmethod
    def _meta(state: RunState) -> dict:
        return {
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "tenant_id": state.tenant_id,
            "user_id": state.user_id,
            "status": state.status.value,
            "created_ts": state.created_ts,
            "finished_ts": state.finished_ts,
        }

    def _load_index(self) -> None:
        self._index = {}
        path = self._index_path()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    meta = json.loads(line)
                    self._index[str(meta["run_id"])] = meta
                except Exception:  # noqa: BLE001 - 坏索引行跳过
                    continue
        else:
            self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._index = {}
        self._counts = {}
        for path in self._run_files():
            try:
                lines = path.read_text(encoding="utf-8").strip().splitlines()
                if not lines:
                    continue
                self._counts[path.stem] = len(lines)
                state = RunState.model_validate_json(lines[-1])
                self._index[state.run_id] = self._meta(state)
            except Exception:  # noqa: BLE001
                continue
        self._write_index()

    def _write_index(self) -> None:
        path = self._index_path()
        body = "\n".join(
            json.dumps(meta, ensure_ascii=False) for meta in self._index.values()
        )
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body + "\n" if body else "", encoding="utf-8")
        tmp.replace(path)

    async def save(self, state: RunState) -> None:
        with self._path(state.run_id).open("a", encoding="utf-8") as handle:
            handle.write(state.model_dump_json() + "\n")
        self._counts[state.run_id] = self._counts.get(state.run_id, 0) + 1
        self._index[state.run_id] = self._meta(state)
        self._write_index()
        if (
            self.compact_enabled
            and self._counts[state.run_id] > self.compact_threshold_lines
        ):
            self._compact(state.run_id)

    def _compact(self, run_id: str) -> None:
        path = self._path(run_id)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) <= self.compact_keep:
            return
        path.write_text("\n".join(lines[-self.compact_keep :]) + "\n", encoding="utf-8")
        self._counts[run_id] = self.compact_keep

    async def load(self, run_id: str) -> RunState | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return None
        return RunState.model_validate_json(lines[-1])

    async def list_thread(self, thread_id: str) -> list[RunState]:
        result: list[RunState] = []
        for run_id, meta in self._index.items():
            if meta.get("thread_id") == thread_id:
                state = await self.load(run_id)
                if state is not None:
                    result.append(state)
        return result

    async def list_runs(self, limit: int = 50) -> list[RunState]:
        metas = sorted(
            self._index.values(),
            key=lambda meta: (-float(meta.get("created_ts", 0.0)), str(meta["run_id"])),
        )
        result: list[RunState] = []
        for meta in metas[: max(0, limit)]:
            state = await self.load(str(meta["run_id"]))
            if state is not None:
                result.append(state)
        return result
