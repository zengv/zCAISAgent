import calendar
import re
from datetime import date, timedelta
from itertools import combinations, permutations
from typing import Literal

from zcaisagent.domain.enums import Support
from zcaisagent.domain.models import Citation, Claim, RunState, Verdict
from zcaisagent.domain.patch import StatePatch
from zcaisagent.domain.text import content_tokens, numbers_in, overlap_ratio
from zcaisagent.orchestration.runtime import RunContext

_MIN_SUPPORT = 0.55
_MIN_PASS = 0.6
_MIN_FAITHFUL = 0.55
_SENTENCE_SPLIT = re.compile(r"[。！？；!?;\n]+")
Action = Literal["respond", "repair_retrieve", "repair_rewrite", "abstain"]

# 只有带推导语义的 claim 才允许数字不一致，防止把校验整体放宽。
_DERIVE_MARKERS = (
    "合计", "共计", "总计", "累计", "之和", "相差", "差额", "应发", "应赔",
    "赔偿", "报销", "补贴", "上限", "最高", "最多", "最少", "折算", "比例",
    "最晚", "截止", "到期", "起算", "之前", "前提交", "前确认",
)
_DATE_RE = re.compile(
    r"(?:(?P<y1>\d{4})\s*年\s*)?(?P<m1>\d{1,2})\s*月\s*(?P<d1>\d{1,2})\s*日"
    r"|(?P<y2>\d{4})-(?P<m2>\d{1,2})-(?P<d2>\d{1,2})"
)
# 排除 "YYYY 年 M 月 D 日" 中的年份/月，避免把日期本身当成期限。
_DURATION_RE = re.compile(
    r"(?<!\d)(\d{1,3})\s*(天|日|个?月)(?!\s*\d{1,2}\s*日)"
    r"|(?<!\d)(\d{1,3})\s*年(?!\s*\d{1,2}\s*月)"
)
_DEFAULT_YEAR = 2026


def _dates_in(text: str) -> list[date]:
    found: list[date] = []
    for match in _DATE_RE.finditer(text):
        year = int(match.group("y1") or match.group("y2") or _DEFAULT_YEAR)
        month = int(match.group("m1") or match.group("m2") or 0)
        day = int(match.group("d1") or match.group("d2") or 0)
        try:
            found.append(date(year, month, day))
        except ValueError:
            continue
    return found


def _durations_in(text: str) -> list[tuple[int, str]]:
    # 先剥离完整日期，否则 "8 月 31 日" 的 "31 日" 会被误当成 31 天期限，
    # 让编造的日期凭空"推导"出来。
    stripped = _DATE_RE.sub(" ", text)
    found: list[tuple[int, str]] = []
    for first, unit, third in _DURATION_RE.findall(stripped):
        if first:
            found.append((int(first), unit))
        elif third:
            found.append((int(third), "年"))
    return found


def _shift_date(value: date, amount: int, unit: str, sign: int) -> date:
    if unit in ("天", "日"):
        return value + timedelta(days=sign * amount)
    months = sign * amount * (12 if unit == "年" else 1)
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _derived_dates(claim_text: str, sources: str) -> tuple[set[str], list[str]]:
    """验证日期是否由"题设/证据日期 + 期限"经有限步加减得到。

    例：2026-03-01 入职、试用期 6 个月、期满前 30 天 → 2026-08-02。
    """
    claim_dates = _dates_in(claim_text)
    base = _dates_in(sources)
    durations = _durations_in(sources)
    if not claim_dates or not base or not durations:
        return set(), []
    labels: dict[date, str] = {item: item.isoformat() for item in base}
    frontier = list(labels)
    for _ in range(2):
        new_frontier: list[date] = []
        for current in frontier:
            for amount, unit in durations:
                for sign in (-1, 1):
                    try:
                        candidate = _shift_date(current, amount, unit, sign)
                    except (ValueError, OverflowError):
                        continue
                    if candidate in labels:
                        continue
                    labels[candidate] = (
                        f"{labels[current]}{'-' if sign < 0 else '+'}{amount}{unit}"
                    )
                    new_frontier.append(candidate)
        frontier = new_frontier
    covered: set[str] = set()
    basis: list[str] = []
    for item in claim_dates:
        if item not in labels:
            continue
        covered |= {str(item.year), str(item.month), str(item.day)}
        label = labels[item]
        if label == item.isoformat():
            # 与题设/证据日期完全相同，属于直接引用而非推导，不记录推导链。
            continue
        basis.append(f"date:{label}={item.isoformat()}")
    return covered, basis


def _derived_number(target: float, constants: list[float]) -> str | None:
    """用证据/题设数字的明确算式复算目标值；复算不出则视为编造。"""
    values = sorted({value for value in constants if value != 0})[:15]
    tolerance = 0.01
    for a, b in combinations(values, 2):
        # 减法/除法方向敏感，正反两种顺序都要试（24-9=15，9-24 不等于 15）。
        for left, right in ((a, b), (b, a)):
            if abs(left + right - target) <= tolerance:
                return f"{left}+{right}"
            if abs(left - right - target) <= tolerance:
                return f"{left}-{right}"
            if abs(left * right - target) <= tolerance:
                return f"{left}*{right}"
            if right and abs(left / right - target) <= tolerance:
                return f"{left}/{right}"
    # 比例式 a*(b-c)/b 对操作数顺序敏感（总额必须是左操作数），需双向遍历。
    for a, b in permutations(values, 2):
        product = a * b
        for c in values:
            if c and abs(product / c - target) <= tolerance:
                return f"{a}*{b}/{c}"
            if b and abs(a * (b - c) / b - target) <= tolerance:
                return f"{a}*({b}-{c})/{b}"
    for a in values:
        for b in values:
            for c in values:
                for d in values:
                    if abs(a * b + c * d - target) <= tolerance:
                        return f"{a}*{b}+{c}*{d}"
    return None


def _numbers_supported(
    claim_text: str, evidence_texts: list[str], goal: str, evidence_numbers: set[str]
) -> tuple[bool, list[str]]:
    """数字校验：命中证据直接通过；未命中时只放行可复算的派生值。

    放行来源分三类，逐条写入 basis 供追溯：
    - given：数字来自题设（问题本身），不是模型臆造；
    - date：由证据/题设日期与期限推算得到；
    - num：由证据/题设数字经明确算式复算得到。
    """
    missing = numbers_in(claim_text) - evidence_numbers
    if not missing:
        return True, []
    sources = " ".join([goal, *evidence_texts])
    strong: list[str] = []
    weak: list[str] = []
    remaining = set(missing)
    for item in sorted(remaining):
        if item in numbers_in(goal):
            weak.append(f"given:{item}")
            remaining.discard(item)
    covered, date_basis = _derived_dates(claim_text, sources)
    digits_covered = remaining & covered
    if digits_covered:
        strong.append("date:" + ";".join(date_basis))
        remaining -= digits_covered
    # claim 中出现但推导不出的日期分量，视为编造日期，不再交给通用算式兜底，
    # 否则 "3+6=9" 之类的巧合会把错误日期放行。
    claim_date_digits: set[str] = set()
    for found_date in _dates_in(claim_text):
        claim_date_digits |= {
            str(found_date.year),
            str(found_date.month),
            str(found_date.day),
        }
    if remaining & claim_date_digits:
        return False, strong + weak
    if remaining and not any(marker in claim_text for marker in _DERIVE_MARKERS):
        return False, strong + weak
    constants = [float(item) for item in numbers_in(sources)]
    for item in sorted(remaining):
        expression = _derived_number(float(item), constants)
        if expression is None:
            return False, strong + weak
        strong.append(f"num:{expression}={item}")
    return True, strong + weak


def _faithfulness(answer_text: str, evidence: list) -> tuple[float, list[str]]:
    """独立 faithfulness：按答案句子检查内容是否由已检索证据支撑。

    与 citation_support 的分工：
    - citation_support 看 claim 是否绑定有效证据，并校验 claim 中的数字；
    - faithfulness 只看答案文本的内容词元覆盖率，不要求 claim/evidence_id 结构。
    因此两者可以互补：引用合法但正文夹带证据外内容时 faithfulness 降低；
    正文措辞与证据一致但 claim 数字写错时 citation_support 降低。
    注意：推导得到的日期/金额不会逐字出现在证据里，覆盖率口径会低估其支持度，
    该限制已在 PRD 中登记。
    """
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(answer_text) if part.strip()]
    evidence_tokens: set[str] = set()
    for item in evidence:
        evidence_tokens |= content_tokens(f"{item.title or ''} {item.content}")
    scored = 0
    grounded = 0
    unsupported: list[str] = []
    for sentence in sentences:
        tokens = content_tokens(sentence)
        if not tokens:
            continue
        scored += 1
        coverage = len(tokens & evidence_tokens) / len(tokens)
        if coverage >= _MIN_FAITHFUL:
            grounded += 1
        else:
            unsupported.append(f"sentence_unsupported:coverage={coverage:.2f}")
    score = grounded / scored if scored else 0.0
    return score, unsupported


async def verify_node(state: RunState, ctx: RunContext) -> StatePatch:
    if state.answer is None or not state.answer.text.strip():
        return StatePatch(
            verdict=Verdict(
                passed=False,
                issues=["empty_answer"],
                suggested_action="abstain",
            )
        )

    evidence_by_id = {item.id: item for item in state.evidence}
    claims: list[Claim] = []
    citations: dict[str, Citation] = {}
    supported = 0
    invalid_citation = False
    number_conflict = False
    for claim in state.answer.claims:
        valid = [eid for eid in claim.evidence_ids if eid in evidence_by_id]
        if any(eid not in evidence_by_id for eid in claim.evidence_ids):
            invalid_citation = True
        if not valid:
            claims.append(
                claim.model_copy(
                    update={"support": Support.UNSUPPORTED, "note": "no_valid_evidence"}
                )
            )
            continue
        ratio = max(overlap_ratio(claim.text, evidence_by_id[eid].content) for eid in valid)
        evidence_numbers: set[str] = set()
        for eid in valid:
            evidence_numbers |= numbers_in(evidence_by_id[eid].content)
        numbers_ok, derivation = _numbers_supported(
            claim.text,
            [evidence_by_id[eid].content for eid in valid],
            state.goal,
            evidence_numbers,
        )
        if ratio >= _MIN_SUPPORT and numbers_ok:
            support = Support.SUPPORTED
            supported += 1
        else:
            support = Support.PARTIAL
        note = f"overlap={ratio:.2f}"
        if not numbers_ok:
            # 只有文本本身有支撑（ratio 达标）、仅数字对不上时才算"数值不一致"；
            # 文本覆盖率不足时属于证据不足，避免把 no_answer 题误归因为数值冲突。
            if ratio >= _MIN_SUPPORT:
                note += ";number_mismatch"
                number_conflict = True
            else:
                note += ";weak_overlap_with_number_conflict"
        elif derivation:
            note += ";derived:" + "|".join(derivation[:3])
        claims.append(claim.model_copy(update={"support": support, "note": note}))
        for eid in valid:
            quote = evidence_by_id[eid].content[:160]
            previous = citations.get(eid)
            quote_confidence = round(ratio, 3)
            if previous is None or previous.confidence < quote_confidence:
                citations[eid] = Citation(
                    evidence_id=eid, quote=quote, confidence=quote_confidence
                )

    total = len(state.answer.claims)
    citation_support = supported / total if total else 0.0
    skill = state.analysis.get("skill") or {}
    min_pass = _MIN_PASS
    if skill.get("matched") and skill.get("min_citation_support"):
        # 技能策略只能收紧，不能低于基线。
        min_pass = max(min_pass, float(skill["min_citation_support"]))
    issues: list[str] = []
    if not state.evidence:
        issues.append("no_evidence")
    if total == 0:
        issues.append("no_claims")
    if invalid_citation:
        issues.append("invalid_evidence_id")
    if total and citation_support < min_pass:
        issues.append("insufficient_citation_support")
    if number_conflict:
        # 数值不一致是硬错误：即使 claim 通过率达标，也不能交付。
        issues.append("number_mismatch")
    passed = (
        bool(state.evidence)
        and total > 0
        and citation_support >= min_pass
        and not invalid_citation
        and not number_conflict
    )

    sufficiency = state.analysis.get("sufficiency") or {}
    missing = sufficiency.get("missing_aspects") or []
    limitations: list[str] = list(missing)
    if sufficiency.get("decision") == "conflict":
        limitations.append("证据间存在冲突，已并排保留")
    if number_conflict:
        limitations.append("数值或日期无法从证据确认")
    if not passed:
        limitations.append("部分结论缺少充分证据")

    if passed and not missing and sufficiency.get("decision") != "conflict" and supported == total:
        confidence = "high"
    elif passed:
        confidence = "medium"
    else:
        confidence = "low"

    if passed:
        suggested_action: Action = "respond"
    elif not state.evidence or total == 0:
        suggested_action = "abstain"
    else:
        suggested_action = "repair_retrieve"

    faithfulness, unsupported = _faithfulness(state.answer.text, state.evidence)
    verdict = Verdict(
        passed=passed,
        faithfulness=round(faithfulness, 4),
        citation_support=citation_support,
        issues=issues,
        suggested_action=suggested_action,
    )
    answer = state.answer.model_copy(
        update={
            "claims": claims,
            "citations": list(citations.values()),
            "confidence": confidence,
            "limitations": limitations,
        }
    )
    return StatePatch(
        answer=answer,
        verdict=verdict,
        append_notes=[
            f"verified:{'pass' if passed else 'fail'}:" + ",".join(issues),
            f"faithfulness:{faithfulness:.2f}",
            *unsupported,
            *[f"claim:{claim.id}:{claim.support}" for claim in claims],
        ],
    )
