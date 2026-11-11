import re

# 顺序有意义：手机号 / 身份证 / 邮箱先跑，避免被更宽的银行卡号规则先吃掉。
_PII_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
    (re.compile(r"(?<![\dXx])\d{17}[\dXx](?![\dXx])"), "[REDACTED_ID_CARD]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[REDACTED_BANK_CARD]"),
)


def redact_pii(text: str) -> str:
    """把手机号、身份证号、邮箱、银行卡号替换为占位符，其余文本保持不变。"""
    if not text:
        return text
    redacted = text
    for pattern, placeholder in _PII_RULES:
        redacted = pattern.sub(placeholder, redacted)
    return redacted
