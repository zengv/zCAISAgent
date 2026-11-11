import json
import re

from zcaisagent.capabilities.conversation.context import resolve_followup
from zcaisagent.domain.enums import AutonomyLevel, MemoryKind
from zcaisagent.domain.models import Message, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.ports.llm import LLMRequest
from zcaisagent.ports.memory import MemoryQuery

_GREETINGS = ("你好", "您好", "hi", "hello", "谢谢", "thanks", "早上好")
_UNSAFE_MARKERS = (
    "忽略以上指令",
    "忽略之前",
    "ignore previous",
    "ignore all previous",
    "绕过权限",
    "越权访问",
    "导出全部密钥",
)
_COMPARE_MARKERS = ("对比", "比较", "区别", "差异", " vs", "vs ")
_HOWTO_MARKERS = ("怎么操作", "如何操作", "操作步骤", "办理流程", "怎么申请", "如何申请")
_ANALYSIS_MARKERS = ("分析", "评估", "调研", "方案", "报告", "规划", "论证")
_REALTIME_MARKERS = ("现在", "今天", "实时", "最新", "当前")
_RECENT_MARKERS = ("最近", "近期", "上个季度", "这个月", "本月")
_FACT_MARKERS = (
    "是什么",
    "多少",
    "怎么",
    "如何",
    "哪些",
    "哪一",
    "哪个",
    "哪里",
    "多久",
    "多长",
    "几天",
    "几个",
    "几号",
    "几点",
    "几周",
    "几个月",
    "几年",
    "几次",
    "什么时候",
    "什么比例",
    "什么客户端",
    "什么标准",
    "什么",
    "谁",
    "是否",
    "能否",
    "可否",
)
_MULTI_HOP_MARKERS = ("分别", "以及", "和", "与", "还有", "、", "；", ";")
# 需要先取数再计算/推算的题目，表面只有一个疑问词，但仍属多跳。
_DERIVED_MARKERS = ("合计", "相差", "应发", "共计", "总计", "累计", "之和", "差额")
_TEMPORAL_MARKERS = ("最晚", "截止", "到期", "起算", "之前", "前提交", "前确认")
_DATE_PATTERN = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")

_INTENTS = {"闲聊", "查事实", "对比", "操作指引", "分析", "不安全"}
_COMPLEXITIES = {"simple", "multi_hop", "open"}
_FRESHNESS = {"none", "recent", "realtime"}
_MODEL_FALLBACK_CONFIDENCE = 0.5

_MODEL_SYSTEM = (
    "你是意图分析器。只输出 JSON：{\"intent\": \"闲聊|查事实|对比|操作指引|分析|不安全\", "
    "\"complexity\": \"simple|multi_hop|open\", \"freshness\": \"none|recent|realtime\", "
    "\"needs_tool\": true|false, \"needs_clarify\": true|false, "
    "\"language\": \"zh|en\", \"confidence\": 0~1}。"
)


def _hit(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _question_marker_count(text: str) -> int:
    """统计问句里出现的独立事实维度数（长标记优先匹配，避免重复计数）。"""
    rest = text
    count = 0
    for marker in sorted(_FACT_MARKERS, key=len, reverse=True):
        if marker in rest:
            count += 1
            rest = rest.replace(marker, "")
    return count


def _rule_analysis(goal: str) -> dict:
    text = goal.strip()
    lowered = text.lower()
    language = "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in text) else "en"

    if _hit(lowered, _UNSAFE_MARKERS):
        intent, confidence = "不安全", 0.95
    elif len(text) <= 12 and _hit(lowered, _GREETINGS):
        intent, confidence = "闲聊", 0.95
    elif _hit(lowered, _COMPARE_MARKERS):
        intent, confidence = "对比", 0.9
    elif _hit(lowered, _HOWTO_MARKERS):
        intent, confidence = "操作指引", 0.85
    elif _hit(lowered, _ANALYSIS_MARKERS):
        intent, confidence = "分析", 0.85
    elif _hit(lowered, _FACT_MARKERS):
        intent, confidence = "查事实", 0.9
    else:
        intent, confidence = "查事实", 0.45

    if intent in {"对比", "操作指引"}:
        complexity = "multi_hop"
    elif intent == "分析":
        complexity = "open"
    elif intent == "查事实" and (
        text.count("？") > 1
        or text.count("?") > 1
        or _hit(lowered, _MULTI_HOP_MARKERS)
        or _hit(lowered, _DERIVED_MARKERS)
        or (_DATE_PATTERN.search(text) and _hit(lowered, _TEMPORAL_MARKERS))
        or _question_marker_count(text) >= 2
    ):
        complexity = "multi_hop"
    else:
        complexity = "simple"

    if _hit(lowered, _REALTIME_MARKERS):
        freshness = "realtime"
    elif _hit(lowered, _RECENT_MARKERS):
        freshness = "recent"
    else:
        freshness = "none"

    return {
        "intent": intent,
        "complexity": complexity,
        "freshness": freshness,
        "needs_tool": intent == "操作指引",
        "needs_clarify": False,
        "language": language,
        "confidence": confidence,
        "basis": "rule",
    }


def _level_for(analysis: dict) -> AutonomyLevel:
    if analysis["intent"] == "闲聊":
        return AutonomyLevel.L0_DIRECT
    # 开放型分析（调研/方案/报告/规划等）需要多子问题探索，归 L3；
    # 对比与多跳查事实仍归 L2。
    if analysis["complexity"] == "open":
        return AutonomyLevel.L3_DEEP
    if analysis["complexity"] == "multi_hop":
        return AutonomyLevel.L2_STANDARD
    return AutonomyLevel.L1_FAST


def _valid_model_analysis(payload: dict) -> dict | None:
    intent = payload.get("intent")
    complexity = payload.get("complexity")
    freshness = payload.get("freshness")
    if intent not in _INTENTS or complexity not in _COMPLEXITIES:
        return None
    if freshness not in _FRESHNESS:
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    return {
        "intent": intent,
        "complexity": complexity,
        "freshness": freshness,
        "needs_tool": bool(payload.get("needs_tool", False)),
        "needs_clarify": False,
        "language": payload.get("language") if payload.get("language") in {"zh", "en"} else "zh",
        "confidence": max(0.0, min(1.0, confidence)),
        "basis": "model",
    }


async def _model_analysis(
    state: RunState, ctx: RunContext
) -> tuple[dict | None, StatePatch | None]:
    if ctx.llm is None:
        return None, None
    request = LLMRequest(
        messages=[
            Message(role="system", content=_MODEL_SYSTEM),
            Message(role="user", content=state.goal),
        ],
        purpose="analyze",
    )
    try:
        response = await ctx.llm.complete(request)
    except Exception:  # noqa: BLE001 - analysis must never fail a run
        return None, None
    match = re.search(r"\{.*\}", response.text, re.S)
    if not match:
        return None, None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    analysis = _valid_model_analysis(payload)
    if analysis is None:
        return None, None
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
    return analysis, StatePatch(budget=budget)


async def analyze_node(state: RunState, ctx: RunContext) -> StatePatch:
    analysis = _rule_analysis(state.goal)
    patch = StatePatch()

    if ctx.guardrail is not None:
        guarded = await ctx.guardrail.check_input(state.goal, ctx.auth)
        if not guarded.allowed:
            analysis.update(intent="不安全", confidence=1.0, basis="rule")
            if guarded.category:
                analysis["unsafe_category"] = guarded.category

    if analysis["confidence"] < _MODEL_FALLBACK_CONFIDENCE:
        fallback, fallback_patch = await _model_analysis(state, ctx)
        if fallback is not None:
            analysis = fallback
            patch = fallback_patch or StatePatch()

    if state.requested_level is not None:
        level = state.requested_level
        analysis["basis"] = "user"
        analysis["requested_level"] = level.value
    else:
        level = _level_for(analysis)

    followup = resolve_followup(state.goal, state.messages)
    if followup and analysis["intent"] not in {"闲聊", "不安全"}:
        analysis["followup_query"] = followup

    notes = [
        f"analyzed:{analysis['intent']}/{analysis['complexity']}",
        f"route_basis:{analysis['basis']}",
    ]
    if ctx.skills is not None:
        decision = ctx.skills.match(state.goal, analysis["intent"], ctx.auth.scopes)
        if decision.skill is not None:
            analysis["skill"] = {
                "matched": True,
                "name": decision.skill.name,
                "version": decision.skill.version,
                "reason": decision.reason,
                "allowed_sources": list(decision.skill.allowed_sources),
                "min_citation_support": decision.skill.min_citation_support,
                "instructions": decision.skill.instructions,
            }
        else:
            analysis["skill"] = {"matched": False, "reason": decision.reason}
        notes.append(f"skill:{decision.skill.name if decision.skill else 'none'}:{decision.reason}")

    # 只读召回 semantic 偏好；记忆异常不影响分析结果。
    memory = getattr(ctx, "memory", None)
    memory_config = getattr(getattr(ctx, "config", None), "memory", None)
    if memory is not None and getattr(memory_config, "enabled", False):
        try:
            recalled = await memory.read(
                MemoryQuery(
                    tenant_id=state.tenant_id,
                    user_id=state.user_id,
                    text=state.goal,
                    kinds=[MemoryKind.SEMANTIC.value],
                    limit=5,
                ),
                ctx.auth,
            )
            analysis["memory"] = {
                "preferences": [item.content for item in recalled],
                "count": len(recalled),
            }
            notes.append(f"memory:recalled:{len(recalled)}")
        except Exception:  # noqa: BLE001 - 记忆读取失败不阻断分析
            analysis["memory"] = {"preferences": [], "count": 0}
            notes.append("memory:error")

    patch.level = level
    patch.analysis = analysis
    patch.append_notes = notes
    return patch
