import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx

from zcaisagent.adapters.events.webhook import WebhookEventSink
from zcaisagent.ports.events import EventEnvelope


def _body(event: EventEnvelope) -> bytes:
    return json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _sleep_recorder() -> tuple[list[float], object]:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    return delays, sleep


def test_publish_signs_body_and_sets_headers() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200)

    event = EventEnvelope(
        type="run.finished",
        tenant_id="demo",
        run_id="run_1",
        data={"b": 1, "a": "中文"},
    )
    sink = WebhookEventSink(
        ["https://hooks.test/a"],
        "top-secret",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(sink.publish(event))

    request = captured["request"]
    body = request.content
    assert body == _body(event)
    assert request.headers["X-ZCAIS-Signature"] == _signature(body, "top-secret")
    assert request.headers["X-ZCAIS-Event-Id"] == event.id
    assert request.headers["X-ZCAIS-Event-Type"] == "run.finished"
    assert request.headers["Content-Type"] == "application/json"


def test_server_errors_retry_until_success() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500 if calls["count"] < 3 else 200)

    delays, sleep = _sleep_recorder()
    sink = WebhookEventSink(
        ["https://hooks.test/a"],
        "s",
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
    asyncio.run(sink.publish(EventEnvelope(type="run.finished")))

    assert calls["count"] == 3
    assert delays == [0.05, 0.1]


def test_too_many_requests_is_retryable() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(429 if calls["count"] == 1 else 200)

    sink = WebhookEventSink(
        ["https://hooks.test/a"],
        "s",
        max_retries=2,
        transport=httpx.MockTransport(handler),
        sleep=_sleep_recorder()[1],
    )
    asyncio.run(sink.publish(EventEnvelope(type="run.finished")))

    assert calls["count"] == 2


def test_client_error_is_not_retried() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400)

    delays, sleep = _sleep_recorder()
    sink = WebhookEventSink(
        ["https://hooks.test/a"],
        "s",
        max_retries=3,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
    asyncio.run(sink.publish(EventEnvelope(type="run.finished")))

    assert calls["count"] == 1
    assert delays == []


def test_exhausted_retries_write_dead_letter(tmp_path: Path) -> None:
    dead_letter = tmp_path / "dead" / "letter.jsonl"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    event = EventEnvelope(type="approval.requested", tenant_id="demo", run_id="run_2")
    sink = WebhookEventSink(
        ["https://hooks.test/bad"],
        "signing-secret",
        max_retries=2,
        dead_letter_path=dead_letter,
        transport=httpx.MockTransport(handler),
        sleep=_sleep_recorder()[1],
    )
    asyncio.run(sink.publish(event))

    lines = dead_letter.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_id"] == event.id
    assert record["type"] == "approval.requested"
    assert record["url"] == "https://hooks.test/bad"
    assert record["attempts"] == 3
    assert record["status"] == 503
    assert record["error"] == "http_503"
    assert "ts" in record
    assert "signing-secret" not in lines[0]


def test_failing_url_does_not_affect_other_urls(tmp_path: Path) -> None:
    calls: dict[str, int] = {"good.test": 0, "bad.test": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls[host] += 1
        return httpx.Response(200 if host == "good.test" else 500)

    dead_letter = tmp_path / "dead_letter.jsonl"
    sink = WebhookEventSink(
        ["https://good.test/hook", "https://bad.test/hook"],
        "s",
        max_retries=1,
        dead_letter_path=dead_letter,
        transport=httpx.MockTransport(handler),
        sleep=_sleep_recorder()[1],
    )
    asyncio.run(sink.publish(EventEnvelope(type="run.finished")))

    assert calls == {"good.test": 1, "bad.test": 2}
    record = json.loads(dead_letter.read_text(encoding="utf-8").splitlines()[0])
    assert record["url"] == "https://bad.test/hook"


def test_network_error_is_retried_and_dead_lettered(tmp_path: Path) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("connection refused")

    dead_letter = tmp_path / "dead_letter.jsonl"
    sink = WebhookEventSink(
        ["https://hooks.test/a"],
        "s",
        max_retries=1,
        dead_letter_path=dead_letter,
        transport=httpx.MockTransport(handler),
        sleep=_sleep_recorder()[1],
    )
    asyncio.run(sink.publish(EventEnvelope(type="run.finished")))

    assert calls["count"] == 2
    record = json.loads(dead_letter.read_text(encoding="utf-8").splitlines()[0])
    assert record["status"] is None
    assert "ConnectError" in record["error"]


def test_dead_letter_write_failure_is_ignored(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    sink = WebhookEventSink(
        ["https://hooks.test/a"],
        "s",
        max_retries=0,
        dead_letter_path=blocker / "dead.jsonl",
        transport=httpx.MockTransport(handler),
        sleep=_sleep_recorder()[1],
    )
    asyncio.run(sink.publish(EventEnvelope(type="run.finished")))
