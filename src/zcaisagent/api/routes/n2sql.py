from fastapi import APIRouter, Header, HTTPException

from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import N2SQLRequest, N2SQLResponse

router = APIRouter()

_ERROR_STATUS = {
    "N2SQL_FORBIDDEN": 403,
    "N2SQL_INVALID_ARGS": 400,
    "N2SQL_INVALID_SQL": 400,
    "N2SQL_LLM_FAILED": 502,
    "N2SQL_DB_FAILED": 502,
}


@router.post("/n2sql", response_model=N2SQLResponse)
async def ask_n2sql(
    request: N2SQLRequest,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> N2SQLResponse:
    container = get_container()
    auth = await container.identity.authenticate(
        x_api_key,
        {"tenant_id": request.tenant_id or "", "user_id": request.user_id},
    )
    service = container.n2sql
    if service is None:
        raise HTTPException(status_code=503, detail="智能问数未启用或数据库未配置")

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")

    result = await service.ask(question, auth)
    if not result.ok:
        status_code = _ERROR_STATUS.get(result.error_code, 400)
        raise HTTPException(
            status_code=status_code,
            detail=result.error_message or result.error_code,
        )
    return N2SQLResponse(
        question=question,
        answer=result.answer,
        sql=result.sql,
        columns=result.columns,
        rows=result.rows,
        row_count=len(result.rows),
        scope=result.scope,
        latency_ms=result.latency_ms,
    )
