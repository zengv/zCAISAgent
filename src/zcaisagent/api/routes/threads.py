from fastapi import APIRouter, Header, HTTPException

from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import ThreadHistoryOut
from zcaisagent.api.service import thread_history

router = APIRouter()

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20


@router.get("/threads/{thread_id}/messages", response_model=ThreadHistoryOut)
async def get_thread_messages(
    thread_id: str,
    limit: int = _DEFAULT_LIMIT,
    before: float | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> ThreadHistoryOut:
    """回放会话历史，供前端刷新后恢复气泡与澄清卡片。

    不存在的会话返回空列表；存在但不属于当前身份统一 403，不泄漏会话是否存在。
    """
    container = get_container()
    auth = await container.identity.authenticate(
        x_api_key,
        {"tenant_id": tenant_id or "", "user_id": user_id or ""},
    )
    try:
        return await thread_history(
            auth,
            thread_id,
            limit=min(max(limit, 1), _MAX_LIMIT),
            before=before,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="thread not accessible") from exc
