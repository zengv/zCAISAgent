"""从 `skills/*/SKILL.md` 加载技能并构建注册表。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from zcaisagent.capabilities.skills.frontmatter import parse_frontmatter
from zcaisagent.domain.models import Skill


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def skill_from_meta(meta: dict[str, Any], body: str) -> Skill:
    if not meta.get("name") or not meta.get("version"):
        raise ValueError("SKILL.md requires 'name' and 'version'")
    support = meta.get("min_citation_support")
    return Skill(
        name=str(meta["name"]),
        version=str(meta["version"]),
        description=str(meta.get("description", "")),
        when_to_use=str(meta.get("when_to_use", "")),
        instructions=body.strip(),
        tags=_as_tuple(meta.get("tags")),
        trigger_keywords=_as_tuple(meta.get("trigger_keywords")),
        allowed_sources=_as_tuple(meta.get("allowed_sources")),
        allowed_tools=_as_tuple(meta.get("allowed_tools")),
        required_scopes=_as_tuple(meta.get("required_scopes")),
        min_citation_support=None if support is None else float(support),
    )


def load_skills(directory: str | Path) -> tuple[list[Skill], list[str]]:
    root = Path(directory)
    skills: list[Skill] = []
    errors: list[str] = []
    if not root.is_dir():
        return skills, errors
    for path in sorted(root.glob("*/SKILL.md")):
        try:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            skills.append(skill_from_meta(meta, body))
        except Exception as exc:  # noqa: BLE001 - 单个技能失败不阻断启动
            errors.append(f"{path}: {exc}")
    return skills, errors


def build_skill_registry(settings: Any) -> Any:
    from zcaisagent.capabilities.skills.registry import (
        InMemorySkillRegistry,
        build_default_skill_registry,
    )

    registry: InMemorySkillRegistry = build_default_skill_registry()
    errors: list[str] = []
    config = getattr(settings, "skills", None)
    if config is not None and getattr(config, "enabled", True):
        loaded, errors = load_skills(config.dir)
        for skill in loaded:
            try:
                registry.register(skill)
            except ValueError:
                errors.append(f"duplicate skill: {skill.name}")
    registry.load_errors = errors  # type: ignore[attr-defined]
    return registry
