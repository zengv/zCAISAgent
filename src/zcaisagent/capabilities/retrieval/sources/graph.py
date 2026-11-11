import hashlib
import json
import time
from pathlib import Path

from zcaisagent.capabilities.retrieval.sources.inmemory import current_versions
from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Chunk, Evidence, Message
from zcaisagent.domain.versions import split_document_id
from zcaisagent.ports.llm import LLMPort, LLMRequest
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuthContext

_MAX_DOC_CHARS = 2000
_MIN_ENTITY_CHARS = 2


def _entity_text(value: object) -> str:
    """模型输出的实体可能是字符串或 {"name": ...} 对象，统一取文本。"""
    if isinstance(value, dict):
        value = value.get("name") or value.get("entity") or value.get("text") or ""
    return str(value).strip()


def _extract_json_object(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("graph: no JSON object in LLM output")
    return json.loads(text[start : end + 1])


class GraphRetriever:
    """实体关系图检索源（GraphRAG 轻量版）：

    懒构建时按文档族调用 LLM 抽取实体与关系，实体按词面锚定到 chunk；
    检索时 query 命中实体后扩展一跳邻居召回关联 chunk。抽取失败自动降级。
    """

    source_name = "graph"
    source_type = SourceType.DOCUMENT

    def __init__(self, llm: LLMPort, timeout_s: float = 60.0, cache_enabled: bool = True) -> None:
        self.llm = llm
        self.timeout_s = timeout_s
        self.cache_enabled = cache_enabled
        self.chunks: list[Chunk] = []
        # 实体 -> 锚定 chunk id；实体 -> 邻居实体（无向边）。
        self._entity_chunks: dict[str, set[str]] = {}
        self._neighbors: dict[str, set[str]] = {}
        self._built = False
        self._failed = False
        # 抽取失败待补抽的文档族；_built 后逐次检索补齐。
        self._missing: set[tuple[str, str | None]] = set()

    def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        self.chunks.extend(chunks)
        self._built = False

    def __len__(self) -> int:
        return len(self._entity_chunks)

    async def _extract_document(self, doc_id: str, version: str | None, chunks: list[Chunk]) -> None:
        body = "\n".join(chunk.content for chunk in chunks)[:_MAX_DOC_CHARS]
        # 极简指令 + 内嵌 JSON 模板：思考型模型对长指令容易思考不收敛导致空输出。
        prompt = (
            f"从文档抽取全部关键实体与实体间关系，只输出JSON "
            '{"entities": [], "relations": []} 格式：\n'
            f"{doc_id}\n{body}"
        )
        response = await self.llm.complete(
            LLMRequest(
                messages=[Message(role="user", content=prompt)],
                purpose="extract",
                max_tokens=4000,
                timeout_s=self.timeout_s,
                # 思考型模型对该任务会无限思考耗尽输出额度，显式关闭。
                disable_thinking=True,
            )
        )
        data = _extract_json_object(response.text)
        names: list[str] = []
        for name in data.get("entities") or []:
            text = _entity_text(name)
            if len(text) >= _MIN_ENTITY_CHARS:
                names.append(text)
        for relation in data.get("relations") or []:
            if not isinstance(relation, (list, tuple)) or len(relation) != 3:
                continue
            head, tail = _entity_text(relation[0]), _entity_text(relation[2])
            if len(head) < _MIN_ENTITY_CHARS or len(tail) < _MIN_ENTITY_CHARS:
                continue
            names.extend([head, tail])
            self._neighbors.setdefault(head, set()).add(tail)
            self._neighbors.setdefault(tail, set()).add(head)
        # 实体按词面锚定到出现它的 chunk（大小写不敏感）。
        before = len(self._entity_chunks)
        lowered = {chunk.id: chunk.content.lower() for chunk in chunks}
        for name in dict.fromkeys(names):
            for chunk_id, content in lowered.items():
                if name.lower() in content:
                    self._entity_chunks.setdefault(name, set()).add(chunk_id)
        if len(self._entity_chunks) == before:
            # 合法 JSON 但零实体同样视为失败，交给上层重试。
            raise ValueError("graph: empty entity extraction")

    def _cache_fingerprint(self) -> str:
        """当前版本语料指纹；语料变化后缓存自动失效。"""
        digest = hashlib.sha256()
        for chunk in current_versions(self.chunks):
            digest.update(chunk.id.encode("utf-8"))
            digest.update(chunk.content.encode("utf-8"))
        return digest.hexdigest()[:16]

    def _cache_path(self) -> Path:
        return Path(".zcaisagent") / "graph_cache.json"

    def _load_cache(self) -> bool:
        if not self.cache_enabled:
            return False
        try:
            data = json.loads(self._cache_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存缺失或损坏时走抽取
            return False
        if data.get("fingerprint") != self._cache_fingerprint():
            return False
        self._entity_chunks = {
            name: set(ids) for name, ids in (data.get("entity_chunks") or {}).items()
        }
        self._neighbors = {
            name: set(ids) for name, ids in (data.get("neighbors") or {}).items()
        }
        self._built = bool(self._entity_chunks)
        return self._built

    def _save_cache(self) -> None:
        if not self.cache_enabled:
            return
        try:
            self._cache_path().parent.mkdir(parents=True, exist_ok=True)
            self._cache_path().write_text(
                json.dumps(
                    {
                        "fingerprint": self._cache_fingerprint(),
                        "entity_chunks": {
                            name: sorted(ids)
                            for name, ids in self._entity_chunks.items()
                        },
                        "neighbors": {
                            name: sorted(ids) for name, ids in self._neighbors.items()
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - 缓存写失败不影响检索
            pass

    async def _ensure_graph(self) -> None:
        if self._failed:
            return
        if self._built:
            if self._missing:
                await self._extract_missing()
            return
        if self._load_cache():
            return
        # 按文档族+版本分组，只为当前版本抽取；串行执行避免免费网关限流。
        groups: dict[tuple[str, str | None], list[Chunk]] = {}
        for chunk in current_versions(self.chunks):
            family, parsed = split_document_id(chunk.doc_id)
            version = chunk.version or parsed
            groups.setdefault((family, version), []).append(chunk)
        for (doc_id, version), group_chunks in groups.items():
            # 思考型模型输出随机收敛，失败时重试；仍失败则留待下次检索补抽。
            for _ in range(4):
                try:
                    await self._extract_document(doc_id, version, group_chunks)
                    break
                except Exception:  # noqa: BLE001 - 单文档抽取失败补抽兜底
                    continue
            else:
                self._missing.add((doc_id, version))
        if not self._entity_chunks:
            if self._missing:
                return  # 全部失败但可补抽，不锁定失败状态。
            self._failed = True
            raise RuntimeError("graph: entity extraction failed")
        self._built = True
        self._save_cache()

    async def _extract_missing(self) -> None:
        pending = sorted(self._missing)
        for doc_id, version in pending:
            group = [
                chunk
                for chunk in current_versions(self.chunks)
                if split_document_id(chunk.doc_id)[0] == doc_id
                and (chunk.version or split_document_id(chunk.doc_id)[1]) == version
            ]
            if not group:
                self._missing.discard((doc_id, version))
                continue
            for _ in range(2):
                try:
                    await self._extract_document(doc_id, version, group)
                    self._missing.discard((doc_id, version))
                    self._save_cache()
                    break
                except Exception:  # noqa: BLE001 - 本轮补抽失败留待下轮
                    continue

    def _match_entities(self, query: str) -> tuple[list[str], list[str]]:
        lowered = query.lower()
        direct = [
            name for name in self._entity_chunks if name.lower() in lowered
        ]
        neighbors: set[str] = set()
        for name in direct:
            for other in self._neighbors.get(name, ()):
                if other in self._entity_chunks and other not in direct:
                    neighbors.add(other)
        return direct, sorted(neighbors)

    async def retrieve(self, req: RetrieveRequest, ctx: AuthContext) -> RetrieveResult:
        started = time.perf_counter()
        try:
            await self._ensure_graph()
        except Exception:  # noqa: BLE001 - 抽取不可用时降级返回
            return RetrieveResult(source_name=self.source_name, degraded=True)

        # 图空时本轮无产出，标记降级（下次检索会补抽失败文档）。
        if not self._entity_chunks:
            return RetrieveResult(source_name=self.source_name, degraded=True)

        direct, neighbor_names = self._match_entities(req.query)
        direct_chunks: set[str] = set()
        for name in direct:
            direct_chunks |= self._entity_chunks[name]
        neighbor_chunks: set[str] = set()
        for name in neighbor_names:
            neighbor_chunks |= self._entity_chunks[name]
        # 命中实体优先，一跳邻居次之；查询即导航。
        candidates = [(chunk_id, 0) for chunk_id in direct_chunks] + [
            (chunk_id, 1) for chunk_id in neighbor_chunks - direct_chunks
        ]
        candidates.sort(key=lambda item: (item[1], item[0]))
        if req.top_k <= 0:
            candidates = []
        candidates = candidates[: req.top_k]

        by_id = {chunk.id: chunk for chunk in self.chunks}
        evidence: list[Evidence] = []
        for rank, (chunk_id, hops) in enumerate(candidates):
            chunk = by_id.get(chunk_id)
            if chunk is None or chunk.tenant_id != req.tenant_id:
                continue
            if req.acl_tokens and chunk.acl and not set(chunk.acl) & set(req.acl_tokens):
                continue
            _, parsed = split_document_id(chunk.doc_id)
            score = round(1.0 / (1.0 + rank), 4)
            evidence.append(
                Evidence(
                    id=chunk.id,
                    source_type=SourceType.DOCUMENT,
                    source_name=self.source_name,
                    title=chunk.title,
                    uri=chunk.uri,
                    content=chunk.content,
                    snippet=chunk.content[:160],
                    score=score,
                    relevance=score,
                    authority="secondary",
                    version=chunk.version or parsed,
                    metadata={
                        "doc_id": chunk.doc_id,
                        "index": chunk.index,
                        "hops": hops,
                    },
                    parent_content=chunk.parent_content,
                )
            )
        return RetrieveResult(
            source_name=self.source_name,
            evidence=evidence,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
