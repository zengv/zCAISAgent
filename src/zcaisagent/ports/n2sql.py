from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from zcaisagent.ports.security import AuthContext


class DataScope(BaseModel):
    """一个用户可见的数据范围；由配置文件定义，服务端强制注入过滤条件。"""

    role: Literal["admin", "teacher", "student"]
    label: str = ""
    student_id: str | None = None
    teacher_name: str | None = None


class N2SQLResult(BaseModel):
    """一次智能问数的完整结果；失败时 ok=False 并带 error_code。"""

    ok: bool
    answer: str = ""
    sql: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    scope: str = ""
    error_code: str = ""
    error_message: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    latency_ms: int = 0
    # 每次 LLM 调用的入参与原始响应（含 system/prompt/response），供轨迹留痕。
    llm_trace: list[dict[str, Any]] = Field(default_factory=list)


class SqlQueryPort(Protocol):
    async def execute(
        self, sql: str, max_rows: int
    ) -> tuple[list[str], list[dict[str, Any]]]: ...


class N2SQLPort(Protocol):
    async def ask(self, question: str, auth: AuthContext) -> N2SQLResult: ...
