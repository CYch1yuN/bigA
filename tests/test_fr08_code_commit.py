"""FR-08 严格回归测试：回测报告 code_commit 非空。

验证：
1. CLI 运行回测后 JSON 输出非空 code_commit
2. Markdown 出现「代码提交号」
3. mock 注入固定 commit (abc1234) 后 JSON/Markdown 包含该值
4. code_commit 不参与 content_hash（改变 code_commit 不影响 content_hash）
5. 直接设置 result.code_commit 后 JSON/Markdown 正确输出
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.models import Signal, Side
from ashare_quant.backtest.report import ReportGenerator
from ashare_quant.cli import main
from tests.backtest_samples import make_quotes, make_trade_dates


# ------------------------------------------------------------------ #
# 辅助
# ------------------------------------------------------------------ #
def _run_engine() -> "object":
    """运行回测，返回 result（不含 code_commit）。"""
    quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
    d = make_trade_dates(date(2024, 1, 2), 10)
    signals = [Signal(d[0], "000001", Side.BUY, 100, "test")]
    engine = BacktestEngine()
    cfg = BacktestConfig(initial_cash=100000)
    from ashare_quant.backtest.strategies import ScriptedStrategy
    return engine.run(
        data=quotes,
        strategy=ScriptedStrategy(signals),
        start_date=d[0],
        end_date=d[-1],
        initial_cash=cfg.initial_cash,
        config=cfg,
    )


# ------------------------------------------------------------------ #
# 1. 直接设置 code_commit 后 JSON/Markdown 正确输出
# ------------------------------------------------------------------ #
class TestCodeCommitInReport:
    """code_commit 设置后出现在 JSON 和 Markdown 中。"""

    def test_json_outputs_code_commit(self):
        """JSON 报告输出非空 code_commit。"""
        result = _run_engine()
        result.code_commit = "abc1234"

        report_gen = ReportGenerator()
        report = report_gen.generate_json(result, Decimal("100000"))

        assert report["code_commit"] == "abc1234"

    def test_markdown_contains_code_commit_label(self):
        """Markdown 包含「代码提交号」标签。"""
        result = _run_engine()
        result.code_commit = "abc1234"

        report_gen = ReportGenerator()
        md = report_gen.generate_markdown(result, Decimal("100000"))

        assert "代码提交号" in md

    def test_markdown_contains_code_commit_value(self):
        """Markdown 包含 code_commit 值。"""
        result = _run_engine()
        result.code_commit = "abc1234"

        report_gen = ReportGenerator()
        md = report_gen.generate_markdown(result, Decimal("100000"))

        assert "abc1234" in md

    def test_json_code_commit_null_when_not_set(self):
        """未设置 code_commit 时 JSON 输出 null。"""
        result = _run_engine()
        # 不设置 code_commit

        report_gen = ReportGenerator()
        report = report_gen.generate_json(result, Decimal("100000"))

        assert report["code_commit"] is None


# ------------------------------------------------------------------ #
# 2. code_commit 不参与 content_hash
# ------------------------------------------------------------------ #
class TestCodeCommitNotInHash:
    """code_commit 是运行元数据，不参与 content_hash。"""

    def test_changing_code_commit_does_not_change_hash(self):
        """改变 code_commit 不影响 content_hash。"""
        result1 = _run_engine()
        result1.code_commit = "aaa111"

        result2 = _run_engine()
        result2.code_commit = "bbb222"

        assert result1.content_hash == result2.content_hash


# ------------------------------------------------------------------ #
# 3. CLI 端到端测试：mock get_code_commit
# ------------------------------------------------------------------ #
class TestCLISetsCodeCommit:
    """CLI 运行回测后设置 code_commit。"""

    def test_cli_sets_code_commit_in_json(self, tmp_path: Path):
        """CLI 运行后 JSON 中 code_commit 非空且等于 mock 值。"""
        # 准备测试数据
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        quotes_path = tmp_path / "quotes.parquet"
        quotes.to_parquet(quotes_path, index=False)

        signals = [
            {"signal_date": "2024-01-02", "symbol": "000001",
             "side": "BUY", "quantity": 100, "reason": "test"}
        ]
        signals_path = tmp_path / "signals.json"
        signals_path.write_text(json.dumps(signals), encoding="utf-8")

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "initial_cash: 100000\n"
            "lot_size: 100\n"
            "commission_rate: 0.0003\n"
            "min_commission: 5.0\n"
            "stamp_duty_rate: 0.001\n"
            "transfer_fee_rate: 0.00002\n"
            "slippage_bps: 5\n",
            encoding="utf-8",
        )

        output_dir = tmp_path / "output"

        # mock get_code_commit 返回固定值
        with patch("ashare_quant.cli.get_code_commit", return_value="abc1234"):
            ret = main([
                "backtest",
                "--quotes", str(quotes_path),
                "--signals", str(signals_path),
                "--config", str(config_path),
                "--strategy", "scripted",
                "--output", str(output_dir),
            ])

        assert ret == 0

        # 验证 JSON
        json_path = output_dir / "backtest-result.json"
        with json_path.open("r", encoding="utf-8") as f:
            report = json.load(f)

        assert report["code_commit"] == "abc1234"

    def test_cli_code_commit_in_markdown(self, tmp_path: Path):
        """CLI 运行后 Markdown 包含 code_commit 值。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        quotes_path = tmp_path / "quotes.parquet"
        quotes.to_parquet(quotes_path, index=False)

        signals = [
            {"signal_date": "2024-01-02", "symbol": "000001",
             "side": "BUY", "quantity": 100, "reason": "test"}
        ]
        signals_path = tmp_path / "signals.json"
        signals_path.write_text(json.dumps(signals), encoding="utf-8")

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "initial_cash: 100000\n"
            "lot_size: 100\n"
            "commission_rate: 0.0003\n"
            "min_commission: 5.0\n"
            "stamp_duty_rate: 0.001\n"
            "transfer_fee_rate: 0.00002\n"
            "slippage_bps: 5\n",
            encoding="utf-8",
        )

        output_dir = tmp_path / "output"

        with patch("ashare_quant.cli.get_code_commit", return_value="abc1234"):
            ret = main([
                "backtest",
                "--quotes", str(quotes_path),
                "--signals", str(signals_path),
                "--config", str(config_path),
                "--strategy", "scripted",
                "--output", str(output_dir),
            ])

        assert ret == 0

        md_path = output_dir / "backtest-report.md"
        md = md_path.read_text(encoding="utf-8")

        assert "代码提交号" in md
        assert "abc1234" in md
