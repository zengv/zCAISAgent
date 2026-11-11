from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from zcaisagent.domain.models import Skill


@dataclass(frozen=True)
class SkillDecision:
    """技能匹配结果；skill 为空表示回退通用策略，reason 用于轨迹解释。"""

    skill: Skill | None
    reason: str


class SkillRegistryPort(Protocol):
    def match(self, goal: str, intent: str, scopes: Sequence[str]) -> SkillDecision:
        ...

    def get(self, name: str) -> Skill | None:
        ...

    def all(self) -> list[Skill]:
        ...
