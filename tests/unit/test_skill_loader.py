import dataclasses
from pathlib import Path

from zcaisagent.capabilities.skills.frontmatter import parse_frontmatter
from zcaisagent.capabilities.skills.loader import build_skill_registry, load_skills
from zcaisagent.settings import Settings, SkillsConfig


def test_parse_frontmatter_scalars_and_lists() -> None:
    text = (
        "---\n"
        "name: demo\n"
        'version: "1.0"\n'
        "enabled: true\n"
        "count: 3\n"
        "tags: [a, b, c]\n"
        "items:\n"
        "  - x\n"
        "  - y\n"
        "---\n"
        "正文内容"
    )
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "demo"
    assert meta["version"] == "1.0"
    assert meta["enabled"] is True
    assert meta["count"] == 3
    assert meta["tags"] == ["a", "b", "c"]
    assert meta["items"] == ["x", "y"]
    assert body.strip() == "正文内容"


def _write_skill(root: Path, name: str, body: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(body, encoding="utf-8")


def test_load_skills_and_build_registry(tmp_path) -> None:
    _write_skill(
        tmp_path,
        "compare",
        "---\n"
        "name: compare\n"
        "version: 1.0.0\n"
        "tags: [对比]\n"
        "trigger_keywords: [比对]\n"
        "required_scopes: [retrieve]\n"
        "---\n"
        "对比说明",
    )
    skills, errors = load_skills(tmp_path)
    assert not errors and skills and skills[0].name == "compare"
    assert skills[0].tags == ("对比",)

    settings = Settings(skills=SkillsConfig(dir=tmp_path))
    registry = build_skill_registry(settings)
    names = {skill.name for skill in registry.all()}
    assert {"enterprise_knowledge_qa", "compare"} <= names
    # tag 命中
    decision = registry.match("帮我对比一下这两项政策", "查事实", ["retrieve"])
    assert decision.skill is not None and decision.reason.startswith(("tag:", "keyword:"))


def test_bad_skill_is_skipped(tmp_path) -> None:
    _write_skill(tmp_path, "broken", "---\nname: broken\n---\n缺少 version")
    skills, errors = load_skills(tmp_path)
    assert not skills
    assert errors and "broken" in errors[0]


def test_skill_requires_scope(tmp_path) -> None:
    _write_skill(
        tmp_path,
        "guarded",
        "---\nname: guarded\nversion: 1.0.0\ntrigger_keywords: [年假]\n"
        "required_scopes: [retrieve]\n---\n正文",
    )
    settings = dataclasses.replace(
        Settings(), skills=SkillsConfig(dir=tmp_path)
    )
    registry = build_skill_registry(settings)
    decision = registry.match("年假多少天", "查事实", [])
    assert decision.skill is None and decision.reason == "permission_denied:retrieve"
