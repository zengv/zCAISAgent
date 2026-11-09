import asyncio
import json

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import CitationOut, RunRequest, RunResponse
from zcaisagent.api.service import execute_run
from zcaisagent.domain.models import RunState
from zcaisagent.domain.stage import ticket_stage
from zcaisagent.ports.security import AuthContext


router = APIRouter()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _to_response(state: RunState) -> RunResponse:
    answer = state.answer
    return RunResponse(
        run_id=state.run_id,
        thread_id=state.thread_id,
        status=state.status.value,
        stage=ticket_stage(state).value,
        answer=answer.text if answer else "",
        confidence=answer.confidence if answer else "low",
        abstained=answer.abstained if answer else True,
        citations=[
            CitationOut(
                evidence_id=citation.evidence_id,
                quote=citation.quote,
                confidence=citation.confidence,
            )
            for citation in (answer.citations if answer else [])
        ],
        evidence_count=len(state.evidence),
        steps=state.step,
        mandate_id=state.mandate.id if state.mandate else None,
        limitations=answer.limitations if answer else [],
    )


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
    return _to_response(state)


@router.post("/chat")
async def chat(
    request: RunRequest,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
):
    auth = await _authenticate(x_api_key, request)
    state = await _execute(auth, request)
    response = _to_response(state)

    async def event_stream():
        yield _sse("meta", {"run_id": state.run_id, "status": state.status.value})
        text = response.answer
        for index in range(0, len(text), 24):
            yield _sse("token", {"text": text[index : index + 24]})
            await asyncio.sleep(0)
        yield _sse(
            "done",
            {
                "run_id": state.run_id,
                "citations": [c.model_dump() for c in response.citations],
                "confidence": response.confidence,
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
