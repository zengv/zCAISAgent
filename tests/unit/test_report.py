import argparse
import json
import os
from datetime import datetime

from zcaisagent import cli
from zcaisagent.evaluation import runner
from zcaisagent.evaluation.report import (
    build_report,
    load_report,
    resolve_report_path,
    write_report,
)


def test_build_report_adds_generated_at_and_cases() -> None:
    raw = {
        "total": 2,
        "failed": 0,
        "correct_rate": 0.95,
        "failures": [{"id": "case-2", "status": "failed"}],
        "by_level": {"L1": {"total": 2}},
    }
    report = build_report(raw, 2, generated_at="2026-01-01T00:00:00+00:00")

    assert report["generated_at"] == "2026-01-01T00:00:00+00:00"
    assert report["cases"] == 2
    for key, value in raw.items():
        assert report[key] == value
    # 原始结果不被就地修改。
    assert "generated_at" not in raw
    assert "cases" not in raw


def test_build_report_defaults_to_utc_now() -> None:
    report = build_report({}, 0)
    assert report["cases"] == 0
    generated = datetime.fromisoformat(report["generated_at"])
    assert generated.tzinfo is not None
    assert generated.utcoffset().total_seconds() == 0


def test_write_report_creates_parent_and_writes_utf8(tmp_path) -> None:
    target = tmp_path / "nested" / "latest.json"
    report = {"generated_at": "2026-01-01T00:00:00+00:00", "cases": 1, "note": "中文"}

    written = write_report(report, target)

    assert written == target
    text = target.read_text(encoding="utf-8")
    assert "中文" in text
    assert "\n  " in text
    assert json.loads(text) == report
    assert not list((tmp_path / "nested").glob("*.tmp"))


def test_write_report_replaces_existing_file(tmp_path) -> None:
    target = tmp_path / "latest.json"
    target.write_text('{"old": true}', encoding="utf-8")

    write_report({"new": True}, target)

    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


def test_load_report_valid_missing_and_invalid(tmp_path) -> None:
    path = tmp_path / "latest.json"
    path.write_text(json.dumps({"total": 1}), encoding="utf-8")
    assert load_report(path) == {"total": 1}

    assert load_report(tmp_path / "absent.json") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_report(broken) is None


def test_resolve_report_path_prefers_primary(tmp_path) -> None:
    primary = tmp_path / "latest.json"
    primary.write_text("{}", encoding="utf-8")
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    newer = baseline_dir / "baseline.json"
    newer.write_text("{}", encoding="utf-8")

    assert resolve_report_path(primary, baseline_dir) == primary


def test_resolve_report_path_falls_back_to_newest(tmp_path) -> None:
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    older = baseline_dir / "older.json"
    latest = baseline_dir / "latest.json"
    older.write_text("{}", encoding="utf-8")
    latest.write_text("{}", encoding="utf-8")
    os.utime(older, (1_000, 1_000))
    os.utime(latest, (2_000, 2_000))
    (baseline_dir / "notes.txt").write_text("ignored", encoding="utf-8")

    resolved = resolve_report_path(tmp_path / "missing.json", baseline_dir)

    assert resolved == latest


def test_resolve_report_path_missing_returns_none(tmp_path) -> None:
    empty_dir = tmp_path / "baselines"
    empty_dir.mkdir()

    assert resolve_report_path(tmp_path / "missing.json") is None
    assert resolve_report_path(tmp_path / "missing.json", empty_dir) is None
    assert resolve_report_path(
        tmp_path / "missing.json", tmp_path / "absent-dir"
    ) is None


def test_cmd_eval_out_writes_report(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "build_container", lambda: object())
    monkeypatch.setattr(runner, "load_cases", lambda path: [{"question": "q"}])

    async def fake_run(container, cases):
        return {"total": 1, "correct_rate": 1.0, "failed": 0}

    monkeypatch.setattr(runner, "run_dataset", fake_run)

    target = tmp_path / "out" / "latest.json"
    args = argparse.Namespace(
        path="data/eval/golden.jsonl", tag=None, level=None, out=str(target)
    )
    assert cli.cmd_eval(args) == 0

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["cases"] == 1
    assert saved["total"] == 1
    assert saved["correct_rate"] == 1.0
    printed = json.loads(capsys.readouterr().out)
    assert printed["generated_at"] == saved["generated_at"]


def test_cmd_eval_without_out_keeps_stdout_plain(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "build_container", lambda: object())
    monkeypatch.setattr(runner, "load_cases", lambda path: [{"question": "q"}])

    async def fake_run(container, cases):
        return {"total": 1}

    monkeypatch.setattr(runner, "run_dataset", fake_run)

    args = argparse.Namespace(path="data/eval/golden.jsonl", tag=None, level=None, out=None)
    assert cli.cmd_eval(args) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == {"total": 1}
    assert not list(tmp_path.iterdir())
