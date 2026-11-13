from zcaisagent.ports.n2sql import DataScope
from zcaisagent.ports.security import AuthContext
from zcaisagent.settings import N2SQLConfig


def resolve_scope(config: N2SQLConfig, auth: AuthContext) -> DataScope | None:
    """按配置文件解析用户数据范围；未知用户或缺少必要字段时返回 None（拒绝）。"""
    entry = config.users.get(auth.user_id)
    if entry is None:
        return None
    if entry.role == "teacher" and not entry.teacher_name:
        return None
    if entry.role == "student" and not entry.student_id:
        return None
    return DataScope(
        role=entry.role,
        label=entry.label or entry.user_id,
        student_id=entry.student_id or None,
        teacher_name=entry.teacher_name or None,
    )
