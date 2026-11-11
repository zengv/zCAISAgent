import asyncio
import json

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import RunRequest, RunResponse
from zcaisagent.api.service import (
    build_run_state,
    execute_run,
    publish_run_events,
    to_response,
)
from zcaisagent.capabilities.memory.distill import distill_and_write
from zcaisagent.capabilities.memory.policies import write_explicit_memory
from zcaisagent.container import build_context
from zcaisagent.domain.models import RunState
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.ports.security import AuditEvent, AuthContext

router = APIRouter()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _authenticate(x_api_key: str, request: RunRequest):
    container = get_container()
    return await container.identity.authenticate(
        x_api_key,
        {"tenant_id": request.tenant_id or "", "user_id": request.user_id},
    )


async def _execute(auth: AuthContext, request: RunRequest) -> RunState:
    try:
        return await execute_run(auth, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        # 不泄漏会话是否存在，统一返回无权限。
        raise HTTPException(status_code=403, detail="thread not accessible") from exc


@router.post("/runs", response_model=RunResponse)
async def create_run(
    request: RunRequest,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> RunResponse:
    auth = await _authenticate(x_api_key, request)
    state = await _execute(auth, request)
    return to_response(state)


@router.post("/chat")
async def chat(
    request: RunRequest,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
):
    auth = await _authenticate(x_api_key, request)
    try:
        state = await build_run_state(auth, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        # 不泄漏会话是否存在，统一返回无权限。
        raise HTTPException(status_code=403, detail="thread not accessible") from exc

    container = get_container()
    ctx = build_context(container, auth)
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    async def emit(event: str, data: dict) -> None:
        await queue.put((event, data))

    ctx.emit = emit

    async def run() -> RunState:
        await write_explicit_memory(container.memory, state.goal, ctx)
        result = await Runtime().execute(build_default_graph(), state, ctx)
        await distill_and_write(container.memory, result, ctx)
        await container.audit.append(
            AuditEvent(
                action="run.finished",
                actor=auth.user_id,
                tenant_id=auth.tenant_id,
                run_id=result.run_id,
                detail={"status": result.status.value, "steps": result.step},
            )
        )
        await publish_run_events(container.events, result)
        return result

    task = asyncio.create_task(run())

    async def event_stream():
        yield _sse(
            "meta",
            {
                "run_id": state.run_id,
                "thread_id": state.thread_id,
                "status": state.status.value,
            },
        )
        try:
            while not (task.done() and queue.empty()):
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=0.05)
                except TimeoutError:
                    continue
                yield _sse(event, data)
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                yield _sse("error", {"message": str(error)})
                return
            result = task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            # 客户端断开时中止后台运行，避免继续消耗模型调用。
            if not task.done():
                task.cancel()

        response = to_response(result)
        yield _sse(
            "done",
            {
                "run_id": response.run_id,
                "status": response.status,
                "answer": response.answer,
                "abstained": response.abstained,
                "citations": [c.model_dump() for c in response.citations],
                "confidence": response.confidence,
                "limitations": response.limitations,
                "metrics": response.metrics.model_dump(),
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
