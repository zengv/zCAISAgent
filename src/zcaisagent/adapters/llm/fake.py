import json
import re

from zcaisagent.ports.llm import LLMDelta, LLMRequest, LLMResponse

_EVIDENCE = re.compile(r'<evidence id="([^"]+)"[^>]*>(.*?)</evidence>', re.S)


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
        yield LLMDelta(text=response.text)
