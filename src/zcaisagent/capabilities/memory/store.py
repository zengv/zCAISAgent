import hashlib
import threading
import time
from dataclasses import dataclass

from zcaisagent.domain.enums import MemoryKind
from zcaisagent.domain.text import content_tokens
from zcaisagent.ports.memory import MemoryItem, MemoryQuery, MemoryWrite
from zcaisagent.ports.security import AuthContext

# working 记忆由 RunState 承担，不进入持久层。
_STORABLE_KINDS = frozenset(
    {
        MemoryKind.EPISODIC.value,
        MemoryKind.SEMANTIC.value,
        MemoryKind.PROCEDURAL.value,
    }
)


@dataclass
class _Entry:
    item: MemoryItem
    created_ts: float
    expires_at: float


def _fingerprint(item: MemoryItem) -> str:
    raw = f"{item.kind}\x00{item.content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _score(text: str, content: str) -> float:
    """关键词/子串相关性；无查询词时全部同分，由写入时间决定顺序。"""
    if not text.strip():
        return 0.0
    lowered = content.lower()
    tokens = content_tokens(text)
    if not tokens:
        return 0.0
    overlap = sum(1 for token in tokens if token in lowered) / len(tokens)
    if text.strip().lower() in lowered:
        overlap += 1.0
    return overlap


class InMemoryMemory:
    """进程内记忆实现：按 (tenant_id, user_id) 隔离，可选 TTL 与条数上限。"""

    def __init__(self, max_items: int = 200, ttl_s: float = 0.0) -> None:
        self.max_items = max(0, int(max_items))
        self.ttl_s = max(0.0, float(ttl_s))
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], list[_Entry]] = {}
        self._fingerprints: dict[tuple[str, str], set[str]] = {}

    @staticmethod
    def _namespace(tenant_id: str, user_id: str) -> tuple[str, str]:
        return (tenant_id or "", user_id or "")

    def _purge(self, namespace: tuple[str, str], now: float) -> list[_Entry]:
        entries = self._entries.get(namespace, [])
        alive = [entry for entry in entries if entry.expires_at <= 0 or entry.expires_at > now]
        if len(alive) != len(entries):
            self._entries[namespace] = alive
            self._fingerprints[namespace] = {_fingerprint(entry.item) for entry in alive}
        return alive

    async def read(self, query: MemoryQuery, ctx: AuthContext) -> list[MemoryItem]:
        tenant_id = query.tenant_id or ctx.tenant_id
        user_id = query.user_id or ctx.user_id
        namespace = self._namespace(tenant_id, user_id)
        kinds = set(query.kinds)
        with self._lock:
            entries = self._purge(namespace, time.time())
            candidates = [
                entry for entry in entries if not kinds or entry.item.kind in kinds
            ]
            ranked = sorted(
                candidates,
                key=lambda entry: (
                    _score(query.text, entry.item.content),
                    entry.created_ts,
                ),
                reverse=True,
            )
            limit = max(0, int(query.limit))
            return [entry.item.model_copy(deep=True) for entry in ranked[:limit]]

    async def write(self, item: MemoryItem, ctx: AuthContext) -> None:
        if item.kind not in _STORABLE_KINDS:
            # working 记忆不落库：直接忽略，不抛异常。
            return
        content = item.content.strip()
        if not content:
            return
        namespace = self._namespace(ctx.tenant_id, ctx.user_id)
        fingerprint = _fingerprint(item.model_copy(update={"content": content}))
        now = time.time()
        with self._lock:
            entries = self._purge(namespace, now)
            seen = self._fingerprints.setdefault(namespace, set())
            if fingerprint in seen:
                return
            entry = _Entry(
                item=item.model_copy(update={"content": content}, deep=True),
                created_ts=now,
                expires_at=now + self.ttl_s if self.ttl_s > 0 else 0.0,
            )
            entries.append(entry)
            seen.add(fingerprint)
            self._entries[namespace] = entries
            self._trim(namespace)

    def _trim(self, namespace: tuple[str, str]) -> None:
        if self.max_items <= 0:
            self._entries[namespace] = []
            self._fingerprints[namespace] = set()
            return
        entries = self._entries[namespace]
        if len(entries) <= self.max_items:
            return
        removed = entries[: len(entries) - self.max_items]
        kept = entries[len(entries) - self.max_items :]
        self._entries[namespace] = kept
        seen = self._fingerprints.setdefault(namespace, set())
        for entry in removed:
            seen.discard(_fingerprint(entry.item))

    async def write_batch(self, items: list[MemoryWrite], ctx: AuthContext) -> None:
        for write in items:
            await self.write(write.item, ctx)

    def all_items(self) -> list[MemoryItem]:
        with self._lock:
            now = time.time()
            items: list[MemoryItem] = []
            for namespace in list(self._entries):
                for entry in self._purge(namespace, now):
                    items.append(entry.item.model_copy(deep=True))
            return items

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._fingerprints.clear()
