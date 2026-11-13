from fastapi.testclient import TestClient

from zcaisagent.api.app import app
from zcaisagent.ports.n2sql import N2SQLResult

client = TestClient(app)


class _FakeService:
    async def ask(self, question, auth):
        return N2SQLResult(
            ok=True,
            answer="一共有 7 门课程。",
            sql="SELECT COUNT(*) AS total FROM courses LIMIT 100",
            columns=["total"],
            rows=[{"total": 7}],
            scope="管理员",
            latency_ms=12,
        )


class _ForbiddenService:
    async def ask(self, question, auth):
        return N2SQLResult(
            ok=False,
            error_code="N2SQL_FORBIDDEN",
            error_message="未配置数据权限",
        )


def test_n2sql_endpoint_returns_rows(offline_api_container, monkeypatch) -> None:
    monkeypatch.setattr(offline_api_container, "n2sql", _FakeService())
    response = client.post(
        "/v1/n2sql",
        json={"question": "一共有多少门课程？", "user_id": "dev"},
        headers={"X-API-Key": "dev-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 1
    assert body["rows"] == [{"total": 7}]
    assert "7" in body["answer"]


def test_n2sql_endpoint_maps_forbidden(offline_api_container, monkeypatch) -> None:
    monkeypatch.setattr(offline_api_container, "n2sql", _ForbiddenService())
    response = client.post(
        "/v1/n2sql",
        json={"question": "我的成绩是多少？", "user_id": "nobody"},
        headers={"X-API-Key": "dev-key"},
    )
    assert response.status_code == 403
