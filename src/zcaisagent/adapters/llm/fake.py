import hashlib
import json
import math
import re

from zcaisagent.domain.text import tokenize
from zcaisagent.ports.llm import LLMDelta, LLMRequest, LLMResponse

_EVIDENCE = re.compile(r'<evidence id="([^"]+)"[^>]*>(.*?)</evidence>', re.S)

_FAKE_DIM = 256


def hash_embedding(text: str, dim: int = _FAKE_DIM) -> list[float]:
    """确定性伪向量：token 哈希映射到固定维度叠加，共同 token 贡献同方向，
    余弦近似 token 重叠度；供测试与离线管道验证使用。"""
    vector = [0.0] * dim
    for token in tokenize(text):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        position = int.from_bytes(digest[:4], "big") % dim
        vector[position] += digest[4] / 255.0 + 0.1
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


class FakeEmbedding:
    """Deterministic hash-based embedding used for local dev and tests."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_embedding(text) for text in texts]


def _first_sentence(text: str) -> str:
    for sep in ("。", "\n", ". "):
        if sep in text:
            return text.split(sep)[0].strip() + (sep if sep == "。" else "")
    return text.strip()[:200]


class FakeLLM:
    """Deterministic extractive LLM used for local dev and smoke tests."""

    async def complete(self, req: LLMRequest) -> LLMResponse:
        user_text = next(
            (m.content for m in reversed(req.messages) if m.role == "user"), ""
        )
        evidence = _EVIDENCE.findall(user_text)
        if evidence:
            claims = []
            for evidence_id, content in evidence[:2]:
                sentence = _first_sentence(content)
                claims.append({"text": sentence, "evidence_ids": [evidence_id]})
            payload = {
                "answer": "根据资料：" + claims[0]["text"],
                "claims": claims,
            }
        else:
            payload = {"answer": f"（模拟直答）{user_text[:120]}", "claims": []}
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            tokens_in=len(user_text),
            tokens_out=len(json.dumps(payload, ensure_ascii=False)),
            cost_usd=0.0,
            model="fake",
        )

    async def stream(self, req: LLMRequest):
        response = await self.complete(req)
        for index in range(0, len(response.text), 24):
            yield LLMDelta(text=response.text[index : index + 24])
        yield LLMDelta(text="", final=response)
