import asyncio

from zcaisagent.capabilities.retrieval.rerank import (
    FallbackReranker,
    LLMPointwiseReranker,
    _parse_rerank_results,
)
from zcaisagent.domain.models import Evidence
from zcaisagent.orchestration.policies.evidence import grade_evidence
from zcaisagent.ports.llm import LLMRequest, LLMResponse


def _evidence(evidence_id: str, content: str, relevance: float = 0.0) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_name=evidence_id.split("#")[0],
        title="测试文档",
        content=content,
        relevance=relevance,
        authority="primary",
    )


class _ScriptedLLM:
    """返回预设文本的桩 LLM，用于验证 rerank 解析与回退路径。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        return LLMResponse(text=self.text, model="stub")

    async def stream(self, req: LLMRequest):
        yield type("Delta", (), {"text": self.text})()


def test_grade_uses_rerank_score_over_lexical_overlap() -> None:
    # 证据与问题词面高度重合（词法 relevance 接近 1.0），但 rerank 判为低相关；
    # rerank 分应直接取代词法分，而不是取 max。
    question = "一线城市住宿标准是多少？"
    item = _evidence("doc#0", f"{question}住宿", relevance=0.0)
    lexical = grade_evidence(question, [item])[0]
    reranked = grade_evidence(question, [item], rerank_scores={"doc#0": 0.05})[0]
    assert lexical.relevance == 1.0
    assert reranked.relevance == 0.05
    assert reranked.final < lexical.final


def test_grade_without_rerank_scores_keeps_lexical_logic() -> None:
    question = "一线城市住宿标准是多少？"
    item = _evidence("doc#0", "一线城市住宿标准为 800 元/晚")
    legacy = grade_evidence(question, [item])
    assert legacy[0].relevance == 1.0
    assert legacy[0].decision == "keep"


def test_reranker_parses_json_array_scores() -> None:
    llm = _ScriptedLLM('```json\n[0.9, 0.1]\n```')
    reranker = LLMPointwiseReranker(llm)
    scores = asyncio.run(reranker.rerank("调休有什么规定？", ["加班可调休", "年会安排"]))
    assert scores == [0.9, 0.1]
    # purpose 映射到 rerank 等级。
    assert llm.requests[0].purpose == "rerank"


def test_reranker_raises_when_output_unparsable() -> None:
    reranker = LLMPointwiseReranker(_ScriptedLLM("我觉得都挺相关的"))
    try:
        asyncio.run(reranker.rerank("q", ["a", "b"]))
        raised = False
    except Exception:
        raised = True
    assert raised


def test_reranker_clamps_out_of_range_scores() -> None:
    reranker = LLMPointwiseReranker(_ScriptedLLM("[1.5, -0.2]"))
    scores = asyncio.run(reranker.rerank("q", ["a", "b"]))
    assert scores == [1.0, 0.0]


def test_parse_rerank_results_orders_by_index() -> None:
    data = {"results": [
        {"index": 1, "relevance_score": 0.4},
        {"index": 0, "relevance_score": 0.9},
    ]}
    assert _parse_rerank_results(data, 2) == [0.9, 0.4]


class _BrokenReranker:
    async def rerank(self, query, documents):
        raise RuntimeError("endpoint down")


class _StubReranker:
    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, query, documents):
        self.calls += 1
        return [0.5] * len(documents)


def test_fallback_reranker_switches_after_primary_failure() -> None:
    fallback = _StubReranker()
    reranker = FallbackReranker(_BrokenReranker(), fallback)
    assert asyncio.run(reranker.rerank("q", ["a"])) == [0.5]
    # 主重排器失败后被记忆跳过，不再重复尝试。
    assert asyncio.run(reranker.rerank("q", ["a"])) == [0.5]
    assert fallback.calls == 2


def test_fallback_reranker_prefers_primary_when_available() -> None:
    primary = _StubReranker()
    fallback = _StubReranker()
    reranker = FallbackReranker(primary, fallback)
    assert asyncio.run(reranker.rerank("q", ["a", "b"])) == [0.5, 0.5]
    assert primary.calls == 1
    assert fallback.calls == 0


def test_rerank_server_returns_jina_style_results() -> None:
    from fastapi.testclient import TestClient

    from zcaisagent.adapters.llm.fake import FakeEmbedding
    from zcaisagent.rerank_server import create_app

    app = create_app(embedder=FakeEmbedding())
    client = TestClient(app)
    resp = client.post(
        "/v1/rerank",
        json={
            "query": "调休有什么规定？",
            "documents": ["加班可申请调休", "年会安排在十二月"],
            "top_n": 1,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1  # top_n=1
    top = data["results"][0]
    assert top["index"] == 0  # 调休相关文档排第一
    assert 0.0 <= top["relevance_score"] <= 1.0


def test_rerank_server_empty_documents() -> None:
    from fastapi.testclient import TestClient

    from zcaisagent.adapters.llm.fake import FakeEmbedding
    from zcaisagent.rerank_server import create_app

    client = TestClient(create_app(embedder=FakeEmbedding()))
    resp = client.post("/v1/rerank", json={"query": "q", "documents": []})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}
