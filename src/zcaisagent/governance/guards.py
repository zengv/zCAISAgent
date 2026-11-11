from zcaisagent.domain.models import Evidence
from zcaisagent.governance.redaction import redact_pii
from zcaisagent.ports.security import AuthContext, GuardrailResult

# 证据与输出侧只拦"提示注入"类标记，避免把制度正文里的词（如"破解软件"）当成攻击。
EVIDENCE_MARKERS = (
    "忽略以上指令",
    "忽略以上所有指令",
    "忽略之前",
    "忽略所有规则",
    "忽略所有指令",
    "ignore previous",
    "ignore all previous",
    "ignore the above",
    "disregard previous",
    "disregard all previous",
    "system prompt",
    "你现在是",
)

# 输入侧注入规则额外覆盖伪造系统指令与越狱措辞；这些词只用于用户输入，
# 不进入证据规则，避免安全制度正文（如"不要视为系统指令"）被误隔离。
_INPUT_INJECTION_MARKERS = (
    *EVIDENCE_MARKERS,
    "忽略引用",
    "系统提示词",
    "系统提示",
    "system:",
    "开发者模式",
    "未经证据支持",
    "当作系统指令",
    "作为系统指令",
    "视为系统指令",
)

# 输入侧按风险类别拦截：注入规避、越权取数、破坏性/绕过管控动作。
_INPUT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("injection", _INPUT_INJECTION_MARKERS),
    (
        "privilege_escalation",
        (
            "其他租户",
            "别的租户",
            "跨租户",
            "其他部门员工",
            "别人的薪资",
            "同事的薪资",
            "薪酬明细",
            "薪资明细",
            "管理员账号",
            "管理员密码",
            "审计日志",
            "财务数据",
        ),
    ),
    (
        "destructive_action",
        (
            "删除知识库",
            "清空知识库",
            "删除所有文档",
            "绕过审批",
            "跳过审批",
            "关闭审计",
        ),
    ),
)


def detect_injection(text: str) -> str | None:
    """命中提示注入标记时返回该标记，否则返回 None。"""
    lowered = text.lower()
    for marker in EVIDENCE_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


def _match_input_rule(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    for category, markers in _INPUT_RULES:
        for marker in markers:
            if marker.lower() in lowered:
                return category, marker
    return None


def classify_input(text: str) -> str | None:
    """返回输入命中的风险类别，未命中时返回 None（供红队评测使用）。"""
    matched = _match_input_rule(text)
    return matched[0] if matched is not None else None


class RuleGuardrail:
    async def check_input(self, text: str, ctx: AuthContext) -> GuardrailResult:
        matched = _match_input_rule(text)
        if matched is None:
            return GuardrailResult(allowed=True)
        category, marker = matched
        return GuardrailResult(
            allowed=False,
            category=category,
            details={"marker": marker},
        )

    async def check_output(self, text: str, ctx: AuthContext) -> GuardrailResult:
        decision = self._check_evidence_text(text)
        if not decision.allowed:
            return decision
        redacted = redact_pii(text)
        if redacted != text:
            return GuardrailResult(
                allowed=False,
                category="pii",
                sanitized_text=redacted,
            )
        return GuardrailResult(allowed=True)

    async def check_evidence(self, evidence: Evidence, ctx: AuthContext) -> GuardrailResult:
        return self._check_evidence_text(evidence.content)

    def _check_evidence_text(self, text: str) -> GuardrailResult:
        marker = detect_injection(text)
        if marker is None:
            return GuardrailResult(allowed=True)
        return GuardrailResult(
            allowed=False,
            category="injection",
            details={"marker": marker},
        )
