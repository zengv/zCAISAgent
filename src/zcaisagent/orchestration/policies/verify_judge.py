"""基于 LLMPort 的语义蕴含判官（NLI judge）。

仅当 ``evaluator.semantic_verify_enabled`` 打开时由容器注入；解析失败/调用异常
返回 ``score=None``，由 verify 节点按 ``fail_open`` 回退词面校验。
"""
from __future__ import annotations

import json
import re
import time

from zcaisagent.domain.models import Message
from zcaisagent.orchestration.telemetry import llm_call_record
from zcaisagent.ports.evaluation import EntailmentResult
from zcaisagent.ports.llm import LLMPort, LLMRequest

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_SYSTEM = (
    "你是事实验证器。判断[结论]是否被[证据]蕴含。"
    '只输出 JSON：{"entailment": 0~1}；1 表示完全被证据支持，0 表示无支持。'
)


class LLMEntailmentJudge:
    def __init__(
        self, llm: LLMPort, purpose: str = "verify", timeout_s: float = 30.0
    ) -> None:
        self.llm = llm
        self.purpose = purpose
        self.timeout_s = timeout_s

    async def entail(self, claim: str, evidence: list[str]) -> EntailmentResult:
        block = "\n".join(f"<evidence>{text}</evidence>" for text in evidence)
        request = LLMRequest(
            messages=[
                Message(role="system", content=_SYSTEM),
                Message(role="user", content=f"结论：{claim}\n证据：\n{block}"),
            ],
            purpose=self.purpose,
            timeout_s=self.timeout_s,
            disable_thinking=True,
        )
        started = time.perf_counter()
        try:
            response = await self.llm.complete(request)
        except Exception:  # noqa: BLE001 - 判官失败回退词面
            return EntailmentResult(score=None, response=None)
        call = llm_call_record(request, response, started)
        match = _JSON_OBJECT.search(response.text)
        if not match:
            return EntailmentResult(score=None, response=response, call=call)
        try:
            payload = json.loads(match.group(0))
            score = float(payload.get("entailment"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return EntailmentResult(score=None, response=response, call=call)
        return EntailmentResult(
            score=max(0.0, min(1.0, score)), response=response, call=call
        )
