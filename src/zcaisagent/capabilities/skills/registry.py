from __future__ import annotations

from typing import Iterable, Sequence

from zcaisagent.domain.models import RunState, Skill
from zcaisagent.ports.skills import SkillDecision


class InMemorySkillRegistry:
    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"skill already registered: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def match(self, goal: str, intent: str, scopes: Sequence[str]) -> SkillDecision:
        if intent in {"闲聊", "不安全"}:
            return SkillDecision(None, f"intent_not_applicable:{intent}")
        granted = set(scopes)
        for skill in self._skills.values():
            hits = [word for word in skill.trigger_keywords if word in goal]
            if not hits:
                continue
            missing = [scope for scope in skill.required_scopes if scope not in granted]
            if missing:
                return SkillDecision(None, "permission_denied:" + ",".join(missing))
            return SkillDecision(skill, f"keyword:{hits[0]}")
        return SkillDecision(None, "no_match")


def allowed_sources(state: RunState) -> list[str]:
    """技能命中时返回源白名单；未命中返回空列表表示不限制。"""
    skill = state.analysis.get("skill") or {}
    if not skill.get("matched"):
        return []
    return [str(item) for item in skill.get("allowed_sources") or []]


def build_default_skill_registry() -> InMemorySkillRegistry:
    registry = InMemorySkillRegistry()
    # 当前只有 inmemory 检索适配器（C3 未接入向量/关键词后端），白名单先收在该源。
    registry.register(
        Skill(
            name="enterprise_knowledge_qa",
            version="1.0.0",
            description="企业制度与流程问答",
            instructions=(
                "只依据白名单来源回答；优先现行版本并注明生效时间或版本；"
                "证据不足时明确说明缺少哪类制度，不要推测。"
            ),
            trigger_keywords=(
                "制度",
                "规定",
                "标准",
                "政策",
                "流程",
                "报销",
                "年假",
                "住宿",
                "加班",
                "考勤",
                "请假",
            ),
            allowed_sources=("inmemory",),
            required_scopes=("retrieve",),
            min_citation_support=0.7,
        )
    )
    return registry
