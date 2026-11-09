from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Answer, Message, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.ports.llm import LLMRequest


async def direct_answer_node(state: RunState, ctx: RunContext) -> StatePatch:
    request = LLMRequest(
        messages=[
            Message(role="system", content="你是 zCAISAgent，回答简洁准确。"),
            Message(role="user", content=state.goal),
        ],
        purpose="direct_answer",
    )
    response = await ctx.llm.complete(request)
    budget = state.budget.model_copy(
        update={
            "used_llm_calls": state.budget.used_llm_calls + 1,
            "used_tokens": state.budget.used_tokens + response.tokens_in + response.tokens_out,
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
