"""FR-15B 编码与可读性回归测试。

验证 Phase 3 正式研究报告（JSON/Markdown）包含必需的可读中文文本，
且不含任何乱码特征片段。同时扫描 src/ 与 reports/phase-3/golden-input/
下的全部 .py/.yaml/.yml/.md/.json 源文件，确保其为有效 UTF-8 且无乱码。

覆盖：
1. Markdown 报告包含必需中文文本（标题、章节、声明）
2. JSON 报告包含必需中文文本（limitations 字段）
3. Markdown 报告无乱码特征片段
4. JSON 报告无乱码特征片段
5. 源文件无乱码特征片段
6. 源文件为有效 UTF-8（可无错解码）
"""
from __future__ import annotations

import json
from pathlib import Path

from ashare_quant.research.report import ResearchReportGenerator
from tests.test_research_report import make_mock_research_result


# ------------------------------------------------------------------ #
# 常量
# ------------------------------------------------------------------ #

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Markdown 报告必需的中文文本（标题、章节名、声明关键词）。
MARKDOWN_REQUIRED_TEXTS: list[str] = [
    "A股双轨策略研究报告",
    "稳健轨",
    "激进轨",
    "蒙特卡洛概率分析",
    "仅用于模拟研究",
]

# JSON 报告必需的中文文本。
# JSON 报告无 Markdown 标题，但 limitations 字段包含关键声明文本，
# 其中 "仅用于模拟研究" 是 FR-15B 重点验证的可读中文片段。
JSON_REQUIRED_TEXTS: list[str] = [
    "仅用于模拟研究",
]

# 乱码特征片段：UTF-8 mojibake 与 GBK mojibake 的典型产物。
GARBLED_FRAGMENTS: list[str] = [
    "Ã",   # UTF-8 mojibake（Latin-1 误读）
    "Æ",   # UTF-8 mojibake
    "锛",  # GBK mojibake（标点乱码）
    "鈥",  # GBK mojibake（标点乱码）
]

# 需扫描的源文件扩展名。
SOURCE_EXTENSIONS: set[str] = {".py", ".yaml", ".yml", ".md", ".json"}

# 需扫描的目录：src/ 源码与 reports/phase-3/golden-input/ 金标准输入。
SOURCE_DIRS: list[Path] = [
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "reports" / "phase-3" / "golden-input",
]


# ------------------------------------------------------------------ #
# 辅助函数
# ------------------------------------------------------------------ #

def _collect_source_files() -> list[Path]:
    """递归收集 src/ 与 golden-input/ 下全部源文件（按路径排序）。"""
    files: list[Path] = []
    for directory in SOURCE_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in SOURCE_EXTENSIONS:
                files.append(path)
    return files


# ------------------------------------------------------------------ #
# 1. Markdown 报告包含必需文本
# ------------------------------------------------------------------ #

def test_markdown_report_contains_required_text():
    """Markdown 报告应包含全部必需的中文文本。"""
    result = make_mock_research_result()
    gen = ResearchReportGenerator()
    md = gen.generate_markdown(result, initial_cash=1000.0)

    missing = [text for text in MARKDOWN_REQUIRED_TEXTS if text not in md]
    assert not missing, f"Markdown 报告缺少必需文本: {missing}"


# ------------------------------------------------------------------ #
# 2. JSON 报告包含必需文本
# ------------------------------------------------------------------ #

def test_json_report_contains_required_text():
    """JSON 报告（序列化为字符串）应包含必需的中文文本。"""
    result = make_mock_research_result()

    # 前置校验：mock 结果的 limitations 列表应包含关键声明文本。
    assert any(
        "仅用于模拟研究" in item for item in result.limitations
    ), "mock ResearchResult.limitations 应包含 '仅用于模拟研究'"

    gen = ResearchReportGenerator()
    summary = gen.generate_json(result, initial_cash=1000.0)
    json_str = json.dumps(summary, ensure_ascii=False)

    missing = [text for text in JSON_REQUIRED_TEXTS if text not in json_str]
    assert not missing, f"JSON 报告缺少必需文本: {missing}"


# ------------------------------------------------------------------ #
# 3. Markdown 报告无乱码
# ------------------------------------------------------------------ #

def test_markdown_report_no_garbled_characters():
    """Markdown 报告不应包含任何乱码特征片段。"""
    result = make_mock_research_result()
    gen = ResearchReportGenerator()
    md = gen.generate_markdown(result, initial_cash=1000.0)

    found = [frag for frag in GARBLED_FRAGMENTS if frag in md]
    assert not found, f"Markdown 报告包含乱码片段: {found}"


# ------------------------------------------------------------------ #
# 4. JSON 报告无乱码
# ------------------------------------------------------------------ #

def test_json_report_no_garbled_characters():
    """JSON 报告（序列化为字符串）不应包含任何乱码特征片段。"""
    result = make_mock_research_result()
    gen = ResearchReportGenerator()
    summary = gen.generate_json(result, initial_cash=1000.0)
    json_str = json.dumps(summary, ensure_ascii=False)

    found = [frag for frag in GARBLED_FRAGMENTS if frag in json_str]
    assert not found, f"JSON 报告包含乱码片段: {found}"


# ------------------------------------------------------------------ #
# 5. 源文件无乱码
# ------------------------------------------------------------------ #

def test_source_files_no_garbled_characters():
    """src/ 与 golden-input/ 下源文件不应包含乱码特征片段。"""
    files = _collect_source_files()
    assert files, "应至少扫描到一个源文件"

    offenders: list[str] = []
    for path in files:
        content = path.read_text(encoding="utf-8")
        for fragment in GARBLED_FRAGMENTS:
            if fragment in content:
                offenders.append(f"{path}: 乱码片段 {fragment!r}")

    assert not offenders, "以下源文件包含乱码:\n" + "\n".join(offenders)


# ------------------------------------------------------------------ #
# 6. 源文件为有效 UTF-8
# ------------------------------------------------------------------ #

def test_source_files_valid_utf8():
    """src/ 与 golden-input/ 下源文件应可被 UTF-8 无错解码。"""
    files = _collect_source_files()
    assert files, "应至少扫描到一个源文件"

    offenders: list[str] = []
    for path in files:
        raw = path.read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(f"{path}: {exc}")

    assert not offenders, "以下源文件不是有效 UTF-8:\n" + "\n".join(offenders)
