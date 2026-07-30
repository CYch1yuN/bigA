"""报告生成模块：质量报告、可复现性报告、覆盖报告。

所有报告由项目代码生成，可追溯到配置、数据版本与代码版本。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .quality import QualityReport


def generate_quality_reports(
    report: QualityReport, reports_dir: str | Path
) -> tuple[Path, Path]:
    """生成 JSON 与 Markdown 质量报告，返回 (json_path, md_path)。"""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "quality-report.json"
    md_path = reports_dir / "quality-report.md"
    json_path.write_text(report.to_json(indent=2), encoding="utf-8")
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path


def generate_reproducibility_report(
    content_hash_value: str,
    second_hash_value: str,
    manifest_path: str | Path | None,
    reports_dir: str | Path,
) -> Path:
    """生成可复现性说明 Markdown。"""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "reproducibility.md"
    stable = content_hash_value == second_hash_value
    lines = [
        "# 可复现性说明 (Phase 1)",
        "",
        "## 目标",
        "",
        "证明相同输入与配置产生相同的 curated 数据与内容哈希；",
        "`fetched_at` 等非确定字段不破坏内容复现测试。",
        "",
        "## 方法",
        "",
        "1. 对同一份合成原始数据运行两次标准化。",
        "2. 计算内容 SHA-256（排除 `fetched_at`）。",
        "3. 比较两次哈希是否一致。",
        "",
        "## 结果",
        "",
        f"- 第一次内容哈希: `{content_hash_value}`",
        f"- 第二次内容哈希: `{second_hash_value}`",
        f"- 哈希一致: **{'是' if stable else '否'}**",
        "",
        "## 非确定字段处理",
        "",
        "`fetched_at` 在内容哈希计算时被排除（见 `config.manifest.content_hash_exclude_fields`），",
        "因此不同抓取时间不会影响复现性结论。",
        "",
    ]
    if manifest_path is not None:
        lines += [
            "## 数据版本清单",
            "",
            f"清单文件: `{manifest_path}`",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def generate_coverage_report(
    coverage_json_path: str | Path | None,
    reports_dir: str | Path,
    pytest_summary: str = "",
) -> Path:
    """从 coverage.json 生成覆盖报告 Markdown。"""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "coverage-report.md"

    lines = [
        "# 测试覆盖报告 (Phase 1)",
        "",
        f"生成时间: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]

    if coverage_json_path is not None and Path(coverage_json_path).exists():
        data = json.loads(Path(coverage_json_path).read_text(encoding="utf-8"))
        totals = data.get("totals", {})
        lines += [
            "## 覆盖率摘要",
            "",
            f"- 语句覆盖率: **{totals.get('percent_covered', 0):.2f}%**",
            f"- 已覆盖语句: {totals.get('covered_lines', 0)}",
            f"- 未覆盖语句: {totals.get('missing_lines', 0)}",
            f"- 总语句数: {totals.get('num_statements', 0)}",
            "",
            "## 各文件覆盖率",
            "",
            "| 文件 | 覆盖率 | 已覆盖 | 总数 |",
            "| --- | --- | --- | --- |",
        ]
        files = data.get("files", {})
        for fname in sorted(files):
            f = files[fname]
            s = f.get("summary", f)
            pct = s.get("percent_covered", 0)
            lines.append(
                f"| {fname} | {pct:.1f}% | {s.get('covered_lines', 0)} | {s.get('num_statements', 0)} |"
            )
    else:
        lines += [
            "## 覆盖率摘要",
            "",
            "未找到 coverage.json（可能未以 --cov 运行）。",
            "",
        ]

    if pytest_summary:
        lines += [
            "## pytest 摘要",
            "",
            "```",
            pytest_summary.strip(),
            "```",
            "",
        ]

    lines += [
        "## 离线测试说明",
        "",
        "所有单元测试离线运行，不调用 AKShare/BaoStock 公网接口。",
        "数据提供器通过 mock/fixture 测试；合成样本覆盖正常、重复、缺失、停牌、退市、",
        "ST 区间、OHLC 错误、负成交量、异常跳变与双源冲突等场景。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


__all__ = [
    "generate_quality_reports",
    "generate_reproducibility_report",
    "generate_coverage_report",
]
