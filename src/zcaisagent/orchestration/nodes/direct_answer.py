import time

from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Answer, Message, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.orchestration.telemetry import emit, llm_call_record
from zcaisagent.ports.llm import LLMRequest, LLMResponse


async def direct_answer_node(state: RunState, ctx: RunContext) -> StatePatch:
    request = LLMRequest(
        messages=[
            Message(role="system", content="你是 zCAISAgent，回答简洁准确。"),
            Message(role="user", content=state.goal),
        ],
        purpose="direct_answer",
    )
    if getattr(ctx, "emit", None) is not None:
        pieces: list[str] = []
        final: LLMResponse | None = None
        started = time.perf_counter()
        async for delta in ctx.llm.stream(request):
            if delta.text:
                pieces.append(delta.text)
                await ctx.emit("token", {"text": delta.text})
            if delta.final is not None:
                final = delta.final
        text = "".join(pieces).strip()
        response = final.model_copy(update={"text": text}) if final else LLMResponse(text=text)
        # 流式的用量由最后一块携带；text 以拼装结果为准。
        record = llm_call_record(request, response, started, text=text)
    else:
        started = time.perf_counter()
        response = await ctx.llm.complete(request)
        record = llm_call_record(request, response, started)
    await emit(ctx, state, "llm.called", node="direct_answer", data=record)
    budget = state.budget.model_copy(
        update={
            "used_llm_calls": state.budget.used_llm_calls + 1,
            "used_tokens": state.budget.used_tokens + response.tokens_in + response.tokens_out,
            "used_tokens_in": state.budget.used_tokens_in + response.tokens_in,
            "used_tokens_out": state.budget.used_tokens_out + response.tokens_out,
            "used_cached_tokens": state.budget.used_cached_tokens + response.cached_tokens,
            "used_cost_usd": state.budget.used_cost_usd + response.cost_usd,
        }
    )
    return StatePatch(
        answer=Answer(
            text=response.text.strip(),
            claims=[],
            confidence="low",
            limitations=["direct_answer_without_evidence"],
        ),
        budget=budget,
        status=RunStatus.SUCCEEDED,
        append_message=Message(role="assistant", content=response.text.strip()),
        append_notes=["direct_answer"],
    )
