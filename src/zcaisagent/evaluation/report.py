"""评估报告产出与读取：latest 报告的统一格式、原子写与路径解析。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_report(
    raw: dict[str, Any], cases: int, generated_at: str | None = None
) -> dict[str, Any]:
    """在 run_dataset 结果上补充生成时间与样本数，保留全部原有字段。"""
    report = dict(raw)
    report["generated_at"] = (
        generated_at or report.get("generated_at") or datetime.now(UTC).isoformat()
    )
    report["cases"] = cases
    return report


def write_report(report: dict[str, Any], path: Path) -> Path:
    """原子写入报告：先写同目录临时文件，再 replace 到目标路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def load_report(path: Path) -> dict[str, Any] | None:
    """读取报告 JSON；文件缺失或内容非法时返回 None。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_report_path(
    primary: Path, fallback_dir: Path | None = None
) -> Path | None:
    """优先返回 primary；缺失时回退 fallback_dir 下 mtime 最新的 *.json。"""
    candidate = Path(primary)
    if candidate.is_file():
        return candidate
    if fallback_dir is None:
        return None
    directory = Path(fallback_dir)
    if not directory.is_dir():
        return None
    reports = [path for path in directory.glob("*.json") if path.is_file()]
    if not reports:
        return None
    return max(reports, key=lambda path: path.stat().st_mtime)
