import asyncio

from zcaisagent.capabilities.n2sql.service import N2SQLService
from zcaisagent.ports.llm import LLMResponse
from zcaisagent.ports.security import AuthContext
from zcaisagent.settings import N2SQLConfig, N2SQLUserScope


class _QueueLLM:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)

    async def complete(self, request):
        return LLMResponse(text=self.texts.pop(0), tokens_in=10, tokens_out=5)


class _Executor:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.sqls: list[str] = []
        self.rows = rows or [{"avg_score": 85.5}]

    async def execute(self, sql, max_rows):
        self.sqls.append(sql)
        return list(self.rows[0].keys()), self.rows


def _auth(user_id: str) -> AuthContext:
    return AuthContext(
        tenant_id="demo",
        user_id=user_id,
        scopes=["tool:read"],
        acl_tokens=["public"],
    )


def _config(**users: N2SQLUserScope) -> N2SQLConfig:
    return N2SQLConfig(
        enabled=True,
        host="localhost",
        port=3306,
        user="u",
        password="p",
        database="d",
        schema="CREATE TABLE scores (score DECIMAL);",
        tables=["courses", "students", "scores"],
        max_rows=100,
        users=users,
    )


def test_admin_question_runs_generated_sql() -> None:
    executor = _Executor()
    service = N2SQLService(
        _config(admin=N2SQLUserScope("admin", "admin", label="管理员")),
        _QueueLLM(
            [
                '{"sql":"SELECT AVG(score) AS avg_score FROM scores","explanation":"平均分"}',
                "全部学生平均分为 85.5 分。",
            ]
        ),
        executor,
    )
    result = asyncio.run(service.ask("所有学生的平均分是多少？", _auth("admin")))
    assert result.ok
    assert result.answer == "全部学生平均分为 85.5 分。"
    assert executor.sqls and "studentID" not in executor.sqls[0]
    assert executor.sqls[0].endswith("LIMIT 100")


def test_student_scope_is_injected_server_side() -> None:
    executor = _Executor([{"score": 90.0}])
    service = N2SQLService(
        _config(s1=N2SQLUserScope("s1", "student", label="S1", student_id="S1")),
        _QueueLLM(['{"sql":"SELECT score FROM scores"}', "你的成绩是 90 分。"]),
        executor,
    )
    result = asyncio.run(service.ask("我的成绩是多少？", _auth("s1")))
    assert result.ok
    assert "studentID = 'S1'" in executor.sqls[0]


def test_unknown_user_is_rejected() -> None:
    executor = _Executor()
    service = N2SQLService(_config(), _QueueLLM([]), executor)
    result = asyncio.run(service.ask("我的成绩是多少？", _auth("nobody")))
    assert not result.ok
    assert result.error_code == "N2SQL_FORBIDDEN"
    assert executor.sqls == []


def test_unsafe_sql_is_rejected() -> None:
    executor = _Executor()
    service = N2SQLService(
        _config(admin=N2SQLUserScope("admin", "admin")),
        _QueueLLM(['{"sql":"DROP TABLE scores"}']),
        executor,
    )
    result = asyncio.run(service.ask("删除成绩表", _auth("admin")))
    assert not result.ok
    assert result.error_code == "N2SQL_INVALID_SQL"
    assert executor.sqls == []
