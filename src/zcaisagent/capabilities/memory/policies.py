import re
import uuid

from zcaisagent.domain.enums import MemoryKind
from zcaisagent.governance.redaction import redact_pii
from zcaisagent.ports.memory import MemoryItem, MemorySource, MemoryWrite
from zcaisagent.ports.security import AuthContext

_EXPLICIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"记住[：:,，]?\s*(.+)", re.S),
    re.compile(r"以后(?:都|请|一直)?[：:,，]?\s*(.+)", re.S),
    re.compile(r"remember(?:\s+that)?[\s:：,，]+(.+)", re.I | re.S),
)

_HIGH_RISK = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|secret|password|passwd|密码|口令|密钥|令牌)"
    r"\s*(?:[:=：]|是|为)\s*\S+"
)
_SECRET_TOKEN = re.compile(
    r"(?i)\b(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,})\b"
)

_REJECTED_SOURCES = frozenset({MemorySource.TOOL, MemorySource.WEB})
_DISTILL_MAX_CHARS = 500
_EXPLICIT_MAX_CHARS = 200


def extract_explicit_memory(text: str) -> str | None:
    """从用户原话中提取显式记忆偏好；无命中返回 None。"""
    if not text:
        return None
    for pattern in _EXPLICIT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        preference = match.group(1).strip().strip("\"'“”‘’").strip()
        preference = preference.rstrip("。！？!?;；")
        if preference:
            return preference[:_EXPLICIT_MAX_CHARS]
    return None


class MemoryWriteGuard:
    """写入策略：来源白名单、空内容、高风险与蒸馏长度约束。"""

    def evaluate(self, write: MemoryWrite, ctx: AuthContext) -> tuple[bool, str]:
        if write.source in _REJECTED_SOURCES:
            return False, "MEMORY_SOURCE_REJECTED"
        content = redact_pii(write.item.content).strip()
        if not content:
            return False, "MEMORY_EMPTY"
        if _HIGH_RISK.search(content) or _SECRET_TOKEN.search(content):
            return False, "MEMORY_HIGH_RISK"
        if write.source == MemorySource.DISTILL:
            if write.item.kind != MemoryKind.EPISODIC.value:
                return False, "MEMORY_DISTILL_KIND_REJECTED"
            if len(content) > _DISTILL_MAX_CHARS:
                return False, "MEMORY_DISTILL_TOO_LONG"
        return True, ""


def prepare_write(
    item: MemoryItem,
    source: MemorySource,
    explicit: bool,
    ctx: AuthContext,
) -> MemoryItem | None:
    """脱敏并做写入策略判定；不允许写入时返回 None。"""
    sanitized = item.model_copy(update={"content": redact_pii(item.content).strip()})
    if not sanitized.content:
        return None
    allowed, _ = MemoryWriteGuard().evaluate(
        MemoryWrite(item=sanitized, source=source, explicit=explicit),
        ctx,
    )
    return sanitized if allowed else None


async def write_explicit_memory(memory, text: str, ctx) -> bool:
    """Run 开始前写入显式偏好；任何失败都不阻断主流程。"""
    try:
        if memory is None:
            return False
        config = getattr(getattr(ctx, "config", None), "memory", None)
        if config is not None and not config.enabled:
            return False
        preference = extract_explicit_memory(text)
        if not preference:
            return False
        auth = getattr(ctx, "auth", ctx)
        item = MemoryItem(
            id=f"sem_{uuid.uuid4().hex[:12]}",
            kind=MemoryKind.SEMANTIC.value,
            content=preference,
            metadata={"source": MemorySource.USER.value, "explicit": True},
        )
        prepared = prepare_write(item, MemorySource.USER, True, auth)
        if prepared is None:
            return False
        await memory.write(prepared, auth)
    except Exception:  # noqa: BLE001 - 记忆失败不得阻断 Run
        return False
    return True
