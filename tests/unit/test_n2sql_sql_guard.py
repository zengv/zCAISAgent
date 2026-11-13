import pytest

from zcaisagent.capabilities.n2sql.errors import N2SQLError
from zcaisagent.capabilities.n2sql.sql_guard import apply_scope, ensure_limit, validate_sql
from zcaisagent.ports.n2sql import DataScope

TABLES = ["courses", "students", "scores"]


def test_validate_rejects_write_and_unknown_tables() -> None:
    with pytest.raises(N2SQLError):
        validate_sql("DELETE FROM scores", TABLES)
    with pytest.raises(N2SQLError):
        validate_sql("SELECT * FROM users", TABLES)

    assert validate_sql("SELECT * FROM scores", TABLES) == "SELECT * FROM scores"


def test_apply_scope_limits_student_to_own_rows() -> None:
    scope = DataScope(role="student", label="S1", student_id="S1")
    sql = apply_scope("SELECT * FROM scores", scope)
    assert "studentID = 'S1'" in sql
    # 库名限定不能绕过过滤。
    qualified = apply_scope("SELECT * FROM study.scores", scope)
    assert "studentID = 'S1'" in qualified
    # 学生可以看课程目录，不被过滤。
    assert apply_scope("SELECT * FROM courses", scope) == "SELECT * FROM courses"


def test_apply_scope_limits_teacher_to_own_courses() -> None:
    scope = DataScope(role="teacher", label="梁文锋", teacher_name="梁文锋")
    sql = apply_scope("SELECT * FROM courses", scope)
    assert "courseTeacher = '梁文锋'" in sql
    scores_sql = apply_scope("SELECT * FROM scores", scope)
    assert "courseTeacher = '梁文锋'" in scores_sql


def test_apply_scope_admin_is_unrestricted() -> None:
    scope = DataScope(role="admin", label="管理员")
    sql = "SELECT * FROM scores"
    assert apply_scope(sql, scope) == sql


def test_ensure_limit_appends_when_missing() -> None:
    assert ensure_limit("SELECT * FROM scores", 100).endswith("LIMIT 100")
    assert ensure_limit("SELECT * FROM scores LIMIT 5", 100).endswith("LIMIT 5")
