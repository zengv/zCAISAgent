import asyncio

from zcaisagent.adapters.checkpoint.jsonl import JsonlCheckpoint
from zcaisagent.domain.models import RunState


def _state(run_id: str, goal: str, created: float, thread: str = "t1") -> RunState:
    return RunState(
        run_id=run_id,
        thread_id=thread,
        tenant_id="demo",
        user_id="u",
        goal=goal,
        created_ts=created,
        finished_ts=created + 1,
    )


def test_compact_keeps_snapshot_under_threshold(tmp_path) -> None:
    cp = JsonlCheckpoint(
        tmp_path, compact_enabled=True, compact_threshold_lines=2, compact_keep=1
    )
    for index in range(4):
        asyncio.run(cp.save(_state("run_a", f"q{index}", float(index))))
    lines = (tmp_path / "run_a.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) <= 2
    assert asyncio.run(cp.load("run_a")).goal == "q3"


def test_list_runs_and_thread_via_index(tmp_path) -> None:
    cp = JsonlCheckpoint(tmp_path)
    asyncio.run(cp.save(_state("run_a", "a", 1.0, thread="t1")))
    asyncio.run(cp.save(_state("run_b", "b", 2.0, thread="t2")))
    runs = asyncio.run(cp.list_runs())
    assert [r.run_id for r in runs] == ["run_b", "run_a"]
    thread = asyncio.run(cp.list_thread("t1"))
    assert [r.run_id for r in thread] == ["run_a"]


def test_index_rebuilt_when_missing(tmp_path) -> None:
    cp = JsonlCheckpoint(tmp_path)
    asyncio.run(cp.save(_state("run_a", "a", 1.0)))
    asyncio.run(cp.save(_state("run_b", "b", 2.0)))
    (tmp_path / ".index.jsonl").unlink()
    rebuilt = JsonlCheckpoint(tmp_path)
    assert {r.run_id for r in asyncio.run(rebuilt.list_runs())} == {"run_a", "run_b"}
