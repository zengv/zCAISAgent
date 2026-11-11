import asyncio
import json

from zcaisagent.operations.fault_drill import run_drills

_SCENARIO_FIELDS = {
    "name",
    "passed",
    "trigger",
    "degraded_path",
    "detail",
    "health",
}


def test_fault_drill_reports_all_scenarios(tmp_path) -> None:
    out = tmp_path / "report.json"
    report = asyncio.run(run_drills(out))

    assert set(report) == {"generated_at", "passed", "scenarios"}
    assert report["passed"] is True
    assert {item["name"] for item in report["scenarios"]} == {
        "model_failure",
        "retrieval_failure",
        "pg_unavailable",
        "redis_unavailable",
    }
    for scenario in report["scenarios"]:
        assert set(scenario) == _SCENARIO_FIELDS
        assert scenario["passed"] is True
        assert scenario["detail"]
        assert scenario["degraded_path"]

    assert out.exists()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written == report

    text = out.read_text(encoding="utf-8")
    assert "sk-p1-drill-secret" not in text
    assert "sk-p2-drill-secret" not in text
    assert "postgresql://u:p@" not in text
