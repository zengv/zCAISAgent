from fastapi import APIRouter, HTTPException

from zcaisagent.api.deps import get_container
from zcaisagent.domain.events import RunEvent

router = APIRouter()


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    container = get_container()
    state = await container.checkpoint.load(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    return state.model_dump()


@router.get("/runs/{run_id}/trace")
async def get_trace(run_id: str):
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
    return {"run_id": run_id, "events": [e.model_dump() for e in events]}
