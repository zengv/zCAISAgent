from fastapi.testclient import TestClient

from zcaisagent.api.app import app

client = TestClient(app)


def test_studio_page_served() -> None:
    response = client.get("/studio")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    html = response.text
    for page in ("sources", "runs", "evals", "config"):
        assert f'data-page="{page}"' in html

    for path in (
        "/v1/admin/sources",
        "/v1/admin/runs",
        "/v1/admin/eval/latest",
        "/v1/admin/config",
        "/v1/admin/metrics",
    ):
        assert path in html


def test_studio_page_has_no_secret_values() -> None:
    html = client.get("/studio").text
    assert "dev-key" in html
    assert "X-API-Key" in html

    for secret in ('"api_key":', "AI_KEY", "postgresql://", '"dsn":'):
        assert secret not in html


def test_root_still_serves_chat_page() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "zCAISAgent · 控制台" in response.text
    assert 'href="/studio"' in response.text
