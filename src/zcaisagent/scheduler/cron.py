"""5 段 cron 解析与匹配（标准库实现，无第三方依赖）。

支持 minute hour day-of-month month day-of-week：
``*``、``*/n``、``a-b``、``a-b/n``、``a,b,c``。
day-of-week 中 0 与 7 都表示周日；dom 与 dow 同时受限时按标准 cron
取「或」语义（任一命中即当天匹配）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

# (下界, 上界)；dow 上界取 7 以兼容「7=周日」。
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool


def _to_int(token: str, lo: int, hi: int, name: str) -> int:
    text = token.strip()
    if not text.isdigit():
        raise ValueError(f"invalid {name} value: {token!r}")
    value = int(text)
    if value < lo or value > hi:
        raise ValueError(f"{name} out of range {lo}-{hi}: {value}")
    return value


def _parse_field(text: str, lo: int, hi: int, name: str, *, dow: bool = False) -> set[int]:
    if not text:
        raise ValueError(f"empty {name} field")
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ValueError(f"empty {name} value in {text!r}")
        base, sep, step_text = part.partition("/")
        step = 1
        if sep:
            if not step_text.isdigit() or int(step_text) <= 0:
                raise ValueError(f"invalid {name} step: {part!r}")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start = _to_int(raw_start, lo, hi, name)
            end = _to_int(raw_end, lo, hi, name)
        else:
            start = _to_int(base, lo, hi, name)
            # ``a/n`` 视为 a..hi/n，与常见实现一致。
            end = hi if sep else start
        if start > end:
            raise ValueError(f"invalid {name} range: {part!r}")
        for value in range(start, end + 1, step):
            values.add(0 if dow and value == 7 else value)
    if not values:
        raise ValueError(f"empty {name} field")
    return values


@lru_cache(maxsize=128)
def parse(expr: str) -> CronSpec:
    """解析表达式；字段数量或取值非法时抛 ValueError。"""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    parsed = [
        _parse_field(field, lo, hi, name, dow=(index == 4))
        for index, (field, (lo, hi), name) in enumerate(
            zip(fields, _FIELD_RANGES, _FIELD_NAMES, strict=True)
        )
    ]
    return CronSpec(
        minutes=frozenset(parsed[0]),
        hours=frozenset(parsed[1]),
        days_of_month=frozenset(parsed[2]),
        months=frozenset(parsed[3]),
        days_of_week=frozenset(parsed[4]),
        dom_restricted=fields[2].strip() != "*",
        dow_restricted=fields[4].strip() != "*",
    )


def _date_matches(spec: CronSpec, dt: datetime) -> bool:
    if dt.month not in spec.months:
        return False
    dom_ok = dt.day in spec.days_of_month
    # isoweekday: 周一=1 ... 周日=7；cron 中周日=0。
    dow_ok = (dt.isoweekday() % 7) in spec.days_of_week
    if spec.dom_restricted and spec.dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def _matches_spec(spec: CronSpec, dt: datetime) -> bool:
    return (
        _date_matches(spec, dt)
        and dt.hour in spec.hours
        and dt.minute in spec.minutes
    )


def matches(expr: str, dt: datetime) -> bool:
    """按 ``dt`` 自身时区判断该分钟是否命中表达式。"""
    return _matches_spec(parse(expr), dt)


def next_after(expr: str, after: datetime, limit_days: int = 366) -> datetime | None:
    """返回严格晚于 ``after`` 的下一个匹配时刻（分钟精度）；无则 None。"""
    spec = parse(expr)
    current = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = current + timedelta(days=limit_days)
    while current <= deadline:
        if not _date_matches(spec, current):
            # 当天日期不匹配：直接跳到次日零点，避免逐分钟空转。
            current = (current + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if current.hour in spec.hours and current.minute in spec.minutes:
            return current
        current += timedelta(minutes=1)
    return None


def in_timezone(dt: datetime, tz_name: str) -> datetime:
    """转换到指定时区；非法时区回退 UTC。"""
    zone = _zone(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone)


@lru_cache(maxsize=64)
def _zone(tz_name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (KeyError, ValueError, OSError):
        return UTC
