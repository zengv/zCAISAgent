from __future__ import annotations

from datetime import UTC, datetime

from zcaisagent.domain.models import Evidence
from zcaisagent.domain.text import numbers_in, overlap_ratio, tokenize
from zcaisagent.ports.evaluation import EvidenceGrade, Sufficiency

_WEIGHTS = {"relevance": 0.45, "authority": 0.25, "freshness": 0.15, "consistency": 0.15}
_AUTHORITY = {"primary": 1.0, "secondary": 0.75, "unknown": 0.5}
_HALF_LIFE_DAYS = 365.0
_DROP_BELOW = 0.30
_QUALIFY_FINAL = 0.40
_QUALIFY_OVERLAP = 0.25
_CONFLICT_SIMILARITY = 0.55
_SUFFICIENCY_THRESHOLD = 0.7

# 不承载问题主题的二元组：疑问虚词、通用业务词、地名/范围定位词。
# 判定时剔除这些片段，只保留能区分具体事实的主题词，避免“公司/标准/补贴/上海”撞词误判为充分。
_NON_TOPIC_BIGRAMS = frozenset(
    {
        "什么", "怎么", "如何", "多少", "几个", "几天", "多久", "是否", "可以",
        "需要", "应该", "请问", "帮我", "告诉", "一下", "时候", "情况", "哪些",
        "公司", "员工", "部门", "同事", "标准", "政策", "规定", "办法", "流程",
        "申请", "报销", "补贴", "费用", "待遇", "额度", "价格", "明细", "余额",
        "服务", "提供", "工作", "时间", "内容", "方面", "相关", "具体", "一般",
        "计算", "提交", "确认", "审批", "完成", "使用", "执行", "处理", "安排",
        "办理", "发放", "给予", "填写", "上传", "查看", "说明", "符合",
        "北京", "上海", "广州", "深圳", "一线", "二线", "三线", "城市", "其他",
        "海外", "国内",
    }
)


_NON_TOPIC_CHARS = frozenset(char for bigram in _NON_TOPIC_BIGRAMS for char in bigram)


def _topic_bigrams(text: str) -> set[str]:
    chars = [ch for ch in text.lower() if "\u4e00" <= ch <= "\u9fff"]
    return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}


def _topic_bigrams_of_question(question: str) -> set[str]:
    # 二元切分会在虚词两侧切出"休有""有什"这类噪声片段，它们不含主题，
    # 若计入分母会把短问句的相关性稀释到门槛以下，这里一并剔除。
    return {
        bigram
        for bigram in _topic_bigrams(question)
        if bigram not in _NON_TOPIC_BIGRAMS and not (set(bigram) & _NON_TOPIC_CHARS)
    }


def _topic_coverage(question: str, text: str) -> float:
    """问题实义主题在证据中的覆盖率。

    疑问虚词与通用业务词不承载主题，若计入分母会把"调休有什么规定"这类
    短问句的相关性稀释到门槛以下；这里只用实义主题二元组衡量覆盖。
    """
    topic = _topic_bigrams_of_question(question)
    if not topic:
        return 0.0
    return len(topic & _topic_bigrams(text)) / len(topic)


def _covers_topic(question: str, text: str) -> bool:
    """证据是否覆盖问题主题：至少一个非通用二元组在证据中完整出现。"""
    if not _topic_bigrams_of_question(question):
        return overlap_ratio(question, text) >= _QUALIFY_OVERLAP
    return _topic_coverage(question, text) > 0


def _jaccard(left: str, right: str) -> float:
    left_tokens = tokenize(left)
    right_tokens = tokenize(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _freshness(evidence: Evidence, now: datetime) -> float:
    if evidence.published_at is None:
        return 0.6
    published = evidence.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    age_days = max(0.0, (now - published).total_seconds() / 86400)
    return 0.5 ** (age_days / _HALF_LIFE_DAYS)


def _conflict_ids(evidence: list[Evidence], similarity: dict[tuple[str, str], float]) -> set[str]:
    """高相似度但数字互斥的证据视为冲突；冲突证据并排保留，不丢弃。"""
    conflicted: set[str] = set()
    for index, left in enumerate(evidence):
        for right in evidence[index + 1 :]:
            if similarity[(left.id, right.id)] < _CONFLICT_SIMILARITY:
                continue
            left_numbers = numbers_in(left.content)
            right_numbers = numbers_in(right.content)
            if left_numbers and right_numbers and left_numbers != right_numbers:
                conflicted.update({left.id, right.id})
    return conflicted


def grade_evidence(
    question: str,
    evidence: list[Evidence],
    now: datetime | None = None,
    rerank_scores: dict[str, float] | None = None,
) -> list[EvidenceGrade]:
    now = now or datetime.now(UTC)
    similarity = {
        (left.id, right.id): _jaccard(left.content, right.content)
        for index, left in enumerate(evidence)
        for right in evidence[index + 1 :]
    }
    conflicted = _conflict_ids(evidence, similarity)
    grades: list[EvidenceGrade] = []
    for item in evidence:
        haystack = f"{item.title or ''} {item.content}"
        if rerank_scores is not None and item.id in rerank_scores:
            # rerank 分直接作为相关性，不与词面重叠取 max，保证语义重排生效。
            relevance = max(0.0, min(1.0, float(rerank_scores[item.id])))
        else:
            relevance = max(
                item.relevance,
                overlap_ratio(question, haystack),
                _topic_coverage(question, haystack),
            )
        authority = _AUTHORITY.get(item.authority, _AUTHORITY["unknown"])
        freshness = _freshness(item, now)
        others = [
            similarity.get((item.id, other.id), similarity.get((other.id, item.id), 0.0))
            for other in evidence
            if other.id != item.id
        ]
        consistency = sum(others) / len(others) if others else 0.6
        final = (
            relevance * _WEIGHTS["relevance"]
            + authority * _WEIGHTS["authority"]
            + freshness * _WEIGHTS["freshness"]
            + consistency * _WEIGHTS["consistency"]
        )
        if item.id in conflicted:
            decision = "conflict"
        elif final >= _DROP_BELOW:
            decision = "keep"
        else:
            decision = "drop"
        grades.append(
            EvidenceGrade(
                evidence_id=item.id,
                relevance=round(relevance, 4),
                authority=round(authority, 4),
                freshness=round(freshness, 4),
                consistency=round(consistency, 4),
                final=round(final, 4),
                decision=decision,
            )
        )
    return grades


def judge_sufficiency(
    question: str,
    evidence: list[Evidence],
    grades: list[EvidenceGrade],
    threshold: float = _SUFFICIENCY_THRESHOLD,
) -> Sufficiency:
    """单问题场景下按"至少一条达标证据即可覆盖"计算覆盖率。"""
    by_id = {grade.evidence_id: grade for grade in grades}
    qualifying = [
        item
        for item in evidence
        if (grade := by_id.get(item.id)) is not None
        and grade.decision != "drop"
        and grade.final >= _QUALIFY_FINAL
        and _covers_topic(question, f"{item.title or ''} {item.content}")
    ]
    coverage = 1.0 if qualifying else 0.0
    has_conflict = any(grade.decision == "conflict" for grade in grades)
    if coverage < threshold:
        decision = "insufficient"
        missing = ["关键实体或维度缺少达标证据"]
    elif has_conflict:
        decision = "conflict"
        missing = []
    else:
        decision = "sufficient"
        missing = []
    return Sufficiency(coverage=coverage, missing_aspects=missing, decision=decision)
