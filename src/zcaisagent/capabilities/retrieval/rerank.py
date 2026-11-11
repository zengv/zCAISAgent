import json

import httpx

from zcaisagent.capabilities.retrieval.sources.vector import cosine
from zcaisagent.domain.models import Message
from zcaisagent.ports.llm import EmbeddingPort, LLMPort, LLMRequest, RerankPort
from zcaisagent.settings import RerankConfig

_MAX_DOC_CHARS = 300


def _extract_json_array(text: str) -> list:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("rerank: no JSON array in LLM output")
    return json.loads(text[start : end + 1])


class LLMPointwiseReranker:
    """复用 LLMPort 一次调用批量打分；解析失败/数量不符时抛异常，调用方回退词法逻辑。"""

    def __init__(self, llm: LLMPort, timeout_s: float = 20.0) -> None:
        self.llm = llm
        self.timeout_s = timeout_s

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        lines = "\n".join(
            f"[{index}] {document[:_MAX_DOC_CHARS]}" for index, document in enumerate(documents)
        )
        prompt = (
            f"问题：{query}\n\n"
            "对下面每段资料与问题的相关程度打分，取值 0 到 1。\n"
            f"{lines}\n\n"
            "只输出一个 JSON 数组，长度与资料条数一致，例如 [0.9, 0.1]。"
        )
        response = await self.llm.complete(
            LLMRequest(
                messages=[Message(role="user", content=prompt)],
                purpose="rerank",
                max_tokens=200,
                timeout_s=self.timeout_s,
            )
        )
        scores = _extract_json_array(response.text)
        if not isinstance(scores, list) or len(scores) != len(documents):
            raise ValueError("rerank: score count mismatch")
        return [max(0.0, min(1.0, float(score))) for score in scores]


class EmbeddingReranker:
    """query-证据语义余弦重排：embedding 服务可用时的首选重排器。"""

    def __init__(self, embedder: EmbeddingPort, timeout_s: float = 30.0) -> None:
        self.embedder = embedder
        self.timeout_s = timeout_s

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        texts = [f"查询：{query}", *documents]
        vectors = await self.embedder.embed(texts)
        query_vector = vectors[0]
        return [
            max(0.0, min(1.0, (cosine(query_vector, vector) + 1.0) / 2.0))
            for vector in vectors[1:]
        ]


def _parse_rerank_results(data: dict, count: int) -> list[float]:
    """兼容 DashScope（output.results）与 Jina/Cohere（顶层 results）格式。"""
    results = data.get("results")
    if results is None and isinstance(data.get("output"), dict):
        results = data["output"].get("results")
    if not isinstance(results, list) or len(results) != count:
        raise ValueError("rerank: results count mismatch")
    ordered = sorted(results, key=lambda item: int(item["index"]))
    return [
        max(0.0, min(1.0, float(item.get("relevance_score", item.get("score", 0.0)))))
        for item in ordered
    ]


class OpenAICompatRerank:
    """专用 rerank 端点适配器；style 决定请求格式（dashscope 原生 / cohere 兼容）。"""

    def __init__(self, config: RerankConfig, timeout_s: float = 30.0) -> None:
        self.config = config
        self.timeout_s = timeout_s

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        if self.config.style == "dashscope":
            payload: dict = {
                "model": self.config.model,
                "input": {"query": query, "documents": documents},
                "parameters": {"return_documents": False, "top_n": len(documents)},
            }
        else:
            payload = {"model": self.config.model, "query": query, "documents": documents}
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.post(
                self.config.base_url, json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
        return _parse_rerank_results(data, len(documents))


class FallbackReranker:
    """优先专用 rerank 端点；端点不可用（404/超时等）后记忆切换到备用重排器。"""

    def __init__(self, primary: RerankPort, fallback: RerankPort) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_down = False

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not self._primary_down:
            try:
                return await self._primary.rerank(query, documents)
            except Exception:  # noqa: BLE001 - 端点不可用时永久切换备用
                self._primary_down = True
        return await self._fallback.rerank(query, documents)
