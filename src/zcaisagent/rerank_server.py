"""本地专用 rerank 端点（Jina/Cohere 兼容格式）。

网关未开放 /rerank 推理端点时，用本服务承接 config.toml [rerank] 的
base_url；内部以网关 embedding 余弦实现相关性打分。将来网关支持专用
rerank API 或本地换 cross-encoder 模型，只需改 base_url，客户端零改动。

启动：python -m zcaisagent rerank-server [--port 8090]
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from zcaisagent.adapters.llm.openai_compat import OpenAICompatEmbedding
from zcaisagent.capabilities.retrieval.sources.vector import cosine
from zcaisagent.ports.llm import EmbeddingPort
from zcaisagent.settings import Settings


class RerankRequest(BaseModel):
    model: str = ""
    query: str
    documents: list[str]
    top_n: int | None = None


def create_app(
    settings: Settings | None = None,
    embedder: EmbeddingPort | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if embedder is None:
        if settings.embedding is None:
            raise RuntimeError("rerank-server requires [embedding] config")
        embedder = OpenAICompatEmbedding(settings.embedding, timeout_s=30.0)
    app = FastAPI(title="zCAISAgent rerank endpoint")

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"data": [{"id": "local-embedding-rerank"}]}

    @app.post("/v1/rerank")
    async def rerank(req: RerankRequest) -> dict[str, Any]:
        if not req.documents:
            return {"results": []}
        texts = [f"查询：{req.query}", *req.documents]
        vectors = await embedder.embed(texts)
        query_vector = vectors[0]
        # 余弦 [-1,1] 归一化到 [0,1]，与客户端 EmbeddingReranker 口径一致。
        scored = sorted(
            (
                (index, (cosine(query_vector, vector) + 1.0) / 2.0)
                for index, vector in enumerate(vectors[1:])
            ),
            key=lambda pair: -pair[1],
        )
        if req.top_n is not None:
            scored = scored[: max(req.top_n, 0)]
        return {
            "results": [
                {"index": index, "relevance_score": round(score, 6)}
                for index, score in scored
            ]
        }

    return app


def main() -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="zcaisagent rerank-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0
