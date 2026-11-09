from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SideEffect = Literal["read", "write", "destructive"]


@dataclass(frozen=True)
class ActionSpec:
    """动作空间条目：声明副作用、所需 scope、超时与幂等性。"""

    name: str
    side_effect: SideEffect
    scopes: tuple[str, ...] = ()
    idempotent: bool = True
    timeout_s: float = 30.0
    # MVP 动作边界：ask_user 与写/破坏性 tool_call 属于 P1。
    mvp: bool = True


_DEFAULT_ACTIONS = (
    ActionSpec("retrieve", "read", ("retrieve",), timeout_s=30.0),
    ActionSpec("tool_call", "read", ("tool:read",), timeout_s=30.0),
    ActionSpec("code_exec", "read", ("code_exec",), idempotent=False, timeout_s=20.0),
    ActionSpec("browse", "read", ("browse",), timeout_s=30.0),
    ActionSpec("file_op", "write", ("file_write",), idempotent=False, timeout_s=20.0),
    ActionSpec("ask_user", "read", (), mvp=False),
    ActionSpec("finish", "read", ()),
)


class ActionSpace:
    def __init__(self) -> None:
        self._specs: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"action already registered: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ActionSpec | None:
        return self._specs.get(name)

    def specs(self) -> list[ActionSpec]:
        return list(self._specs.values())

    def available(self, name: str) -> bool:
        spec = self._specs.get(name)
        return spec is not None and spec.mvp

    def missing_scopes(self, name: str, scopes: list[str]) -> list[str]:
        spec = self._specs.get(name)
        if spec is None:
            return ["<unregistered>"]
        granted: set[str] = set(scopes)
        return [scope for scope in spec.scopes if scope not in granted]


def build_default_action_space() -> ActionSpace:
    space = ActionSpace()
    for spec in _DEFAULT_ACTIONS:
        space.register(spec)
    return space
