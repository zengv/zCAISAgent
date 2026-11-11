import asyncio

import httpx

from zcaisagent.adapters.security.opa import OpaPolicy
from zcaisagent.ports.security import AuthContext, Resource

_CTX = AuthContext(tenant_id="demo", user_id="alice", scopes=["tool:read"])
_RESOURCE = Resource(kind="tool", id="kb_search", tenant_id="demo")


def _policy(handler) -> OpaPolicy:
    return OpaPolicy(
        "https://opa.example/v1/data/zcais/allow",
        transport=httpx.MockTransport(handler),
    )


def _authorize(policy: OpaPolicy):
    return asyncio.run(policy.authorize("tool:read", _RESOURCE, _CTX))


def test_opa_allow_response_allows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": True})

    decision = _authorize(_policy(handler))
    assert decision.allowed is True


def test_opa_false_response_denies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": False})

    decision = _authorize(_policy(handler))
    assert decision.allowed is False
    assert decision.reason == "opa_denied"


def test_opa_error_response_denies_by_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    decision = _authorize(_policy(handler))
    assert decision.allowed is False
    assert decision.reason == "opa_unavailable"


def test_opa_network_error_denies_by_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    decision = _authorize(_policy(handler))
    assert decision.allowed is False
    assert decision.reason == "opa_unavailable"


def test_opa_invalid_payload_denies_by_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    decision = _authorize(_policy(handler))
    assert decision.allowed is False
    assert decision.reason == "opa_unavailable"
