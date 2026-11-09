from zcaisagent.domain.models import Evidence
from zcaisagent.ports.security import AuthContext, GuardrailResult

# 证据与输出侧只拦"提示注入"类标记，避免把制度正文里的词（如"破解软件"）当成攻击。
_EVIDENCE_MARKERS = (
    "忽略以上指令",
    "忽略之前",
    "ignore previous",
    "ignore all previous",
    "system prompt",
    "你现在是",
)

# 输入侧按风险类别拦截：注入规避、越权取数、破坏性/绕过管控动作。
_INPUT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "injection",
        (
            "忽略以上指令",
            "忽略之前",
            "忽略引用",
            "ignore previous",
            "ignore all previous",
            "system prompt",
            "系统提示词",
            "系统提示",
            "你现在是",
            "未经证据支持",
            "当作系统指令",
            "作为系统指令",
            "视为系统指令",
        ),
    ),
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


class RuleGuardrail:
    async def check_input(self, text: str, ctx: AuthContext) -> GuardrailResult:
        lowered = text.lower()
        for category, markers in _INPUT_RULES:
            for marker in markers:
                if marker.lower() in lowered:
                    return GuardrailResult(
                        allowed=False,
                        category=category,
                        details={"marker": marker},
                    )
        return GuardrailResult(allowed=True)

    async def check_output(self, text: str, ctx: AuthContext) -> GuardrailResult:
        return self._check_evidence_text(text)

    async def check_evidence(self, evidence: Evidence, ctx: AuthContext) -> GuardrailResult:
        return self._check_evidence_text(evidence.content)

    def _check_evidence_text(self, text: str) -> GuardrailResult:
        lowered = text.lower()
        for marker in _EVIDENCE_MARKERS:
            if marker.lower() in lowered:
                return GuardrailResult(
                    allowed=False,
                    category="injection",
                    details={"marker": marker},
                )
        return GuardrailResult(allowed=True)
