import asyncio
import json

import httpx
import pytest

from zcaisagent.sdk import (
    ZCAISAPIError,
    ZCAISAuthError,
    ZCAISClient,
    ZCAISConflictError,
    ZCAISForbiddenError,
    ZCAISNotFoundError,
)

_API = "https://api.test"


def _client(handler, **kwargs) -> ZCAISClient:
    return ZCAISClient(
        f"{_API}/",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def _get_run(client: ZCAISClient) -> dict:
    async with client:
        return await client.get_run("run_1")


def test_create_run_posts_payload_and_headers() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"run_id": "run_1", "status": "succeeded"})

    async def scenario() -> dict:
        async with _client(handler) as client:
            return await client.create_run(
                "查一下退款政策",
                thread_id="t1",
                tenant_id="demo",
                level="L1_FAST",
            )

    result = asyncio.run(scenario())

    request = captured["request"]
    assert request.method == "POST"
    assert str(request.url) == f"{_API}/v1/runs"
    assert request.headers["X-API-Key"] == "test-key"
    assert json.loads(request.content) == {
        "goal": "查一下退款政策",
        "thread_id": "t1",
        "tenant_id": "demo",
        "user_id": "dev",
        "level": "L1_FAST",
    }
    assert result == {"run_id": "run_1", "status": "succeeded"}


def test_get_run_and_trace_send_query_params() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/trace"):
            return httpx.Response(200, json={"run_id": "run_1", "events": []})
        return httpx.Response(200, json={"run_id": "run_1"})

    async def scenario() -> tuple[dict, dict]:
        async with _client(handler) as client:
            state = await client.get_run("run_1", tenant_id="demo", user_id="alice")
            trace = await client.trace("run_1", tenant_id="demo")
            await client.get_run("run_1")
        return state, trace

    state, trace = asyncio.run(scenario())

    assert state == {"run_id": "run_1"}
    assert trace == {"run_id": "run_1", "events": []}
    assert requests[0].url.path == "/v1/runs/run_1"
    assert dict(requests[0].url.params) == {"tenant_id": "demo", "user_id": "alice"}
    assert requests[1].url.path == "/v1/runs/run_1/trace"
    assert dict(requests[1].url.params) == {"tenant_id": "demo"}
    assert requests[2].url.query == b""


def test_approve_reject_body_and_cancel_without_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run_id": "run_1", "status": "succeeded"})

    async def scenario() -> None:
        async with _client(handler) as client:
            await client.approve("run_1", reason="同意执行", actor="alice")
            await client.reject("run_1", reason="不允许写")
            await client.cancel("run_1")

    asyncio.run(scenario())

    assert requests[0].url.path == "/v1/runs/run_1/approve"
    assert json.loads(requests[0].content) == {"reason": "同意执行", "actor": "alice"}
    assert requests[1].url.path == "/v1/runs/run_1/reject"
    assert json.loads(requests[1].content) == {"reason": "不允许写"}
    assert requests[2].url.path == "/v1/runs/run_1/cancel"
    assert requests[2].content == b""


def test_ask_n2sql_posts_payload_and_returns_result() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "answer": "S1 的成绩是 92 分。",
                "sql": "SELECT * FROM scores WHERE studentID = 'S1'",
                "columns": ["studentID", "score"],
                "rows": [{"studentID": "S1", "score": 92}],
                "row_count": 1,
                "scope": "student:S1",
            },
        )

    async def scenario() -> dict:
        async with _client(handler) as client:
            return await client.ask_n2sql(
                "S1 的成绩是多少？", user_id="s_s1", tenant_id="demo"
            )

    result = asyncio.run(scenario())

    request = captured["request"]
    assert request.method == "POST"
    assert str(request.url) == f"{_API}/v1/n2sql"
    assert request.headers["X-API-Key"] == "test-key"
    assert json.loads(request.content) == {
        "question": "S1 的成绩是多少？",
        "user_id": "s_s1",
        "tenant_id": "demo",
    }
    assert result["answer"] == "S1 的成绩是 92 分。"
    assert result["row_count"] == 1
    assert result["rows"] == [{"studentID": "S1", "score": 92}]


def test_ask_n2sql_omits_optional_tenant() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"answer": "", "rows": []})

    async def scenario() -> dict:
        async with _client(handler) as client:
            return await client.ask_n2sql("S1 的成绩是多少？", user_id="s_s1")

    asyncio.run(scenario())

    assert json.loads(captured["request"].content) == {
        "question": "S1 的成绩是多少？",
        "user_id": "s_s1",
    }


def test_metrics_and_health_paths() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async def scenario() -> None:
        async with _client(handler) as client:
            await client.metrics()
            await client.metrics(window="7d")
            await client.health()

    asyncio.run(scenario())

    assert requests[0].url.path == "/v1/admin/metrics"
    assert dict(requests[0].url.params) == {"window": "24h"}
    assert dict(requests[1].url.params) == {"window": "7d"}
    assert requests[2].url.path == "/health"
    assert requests[2].headers["X-API-Key"] == "test-key"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, ZCAISAuthError),
        (403, ZCAISForbiddenError),
        (404, ZCAISNotFoundError),
        (409, ZCAISConflictError),
    ],
)
def test_status_maps_to_specific_errors(status: int, error_type: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    with pytest.raises(error_type) as info:
        asyncio.run(_get_run(_client(handler)))

    assert isinstance(info.value, ZCAISAPIError)
    assert info.value.status_code == status
    assert info.value.message == "nope"
    assert info.value.body == {"detail": "nope"}


def test_server_error_uses_api_error_with_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(ZCAISAPIError) as info:
        asyncio.run(_get_run(_client(handler)))

    assert type(info.value) is ZCAISAPIError
    assert info.value.status_code == 500
    assert info.value.message == "boom"
    assert info.value.body == "boom"


_SSE_BODY = (
    'event: meta\ndata: {"run_id": "run_1", "status": "running"}\n\n'
    'event: stage\ndata: {"stage": "plan",\ndata: "step": 1}\n\n'
    "data: [DONE]\n\n"
    'event: partial\ndata: not-json\n\n'
    'event: done\ndata: {"run_id": "run_1", "status": "succeeded"}\n\n'
)


def test_chat_stream_parses_sse_events_in_order() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            text=_SSE_BODY,
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> list[tuple[str, dict]]:
        async with _client(handler) as client:
            return [
                item
                async for item in client.chat_stream(
                    "你好", thread_id="t1", tenant_id="demo"
                )
            ]

    events = asyncio.run(scenario())

    request = captured["request"]
    assert request.method == "POST"
    assert str(request.url) == f"{_API}/v1/chat"
    assert request.headers["X-API-Key"] == "test-key"
    assert json.loads(request.content) == {
        "goal": "你好",
        "thread_id": "t1",
        "tenant_id": "demo",
        "user_id": "dev",
    }
    assert events == [
        ("meta", {"run_id": "run_1", "status": "running"}),
        ("stage", {"stage": "plan", "step": 1}),
        ("done", {"run_id": "run_1", "status": "succeeded"}),
    ]


def test_chat_stream_maps_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid api key"})

    async def scenario() -> list[tuple[str, dict]]:
        async with _client(handler) as client:
            return [item async for item in client.chat_stream("你好")]

    with pytest.raises(ZCAISAuthError) as info:
        asyncio.run(scenario())

    assert info.value.status_code == 401
    assert info.value.message == "invalid api key"


def test_connection_errors_propagate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(httpx.ConnectError):
        asyncio.run(_get_run(_client(handler)))


def test_async_context_closes_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    async def scenario() -> None:
        client = _client(handler)
        async with client:
            await client.health()
            assert client._client is not None
        assert client._client is None

    asyncio.run(scenario())
