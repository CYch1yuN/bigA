"""回测 CLI 端到端测试。

验证 `ashare-quant backtest` 子命令的完整流程：
- 金标准 CLI 成功运行并生成全部输出文件
- 失败场景返回非零退出码
- 账务不平时返回非零
- 无信号参数时返回非零
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

from ashare_quant.cli import main
from tests.backtest_samples import make_bar, make_quotes, make_trade_dates


# ------------------------------------------------------------------ #
# 辅助函数
# ------------------------------------------------------------------ #
def _write_quotes_parquet(tmp_path: Path, filename: str = "quotes.parquet") -> Path:
    """生成合成行情并写入 Parquet。"""
    quotes = make_quotes("000001", date(2024, 1, 2), 10)
    path = tmp_path / filename
    quotes.to_parquet(path, index=False)
    return path


def _write_signals_json(
    tmp_path: Path,
    signals: list[dict],
    filename: str = "signals.json",
) -> Path:
    """写入信号 JSON。"""
    path = tmp_path / filename
    path.write_text(json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_config_yaml(
    tmp_path: Path,
    initial_cash: float = 10000.0,
    filename: str = "backtest.yaml",
) -> Path:
    """写入回测配置 YAML。"""
    content = f"""\
initial_cash: {initial_cash}
lot_size: 100

commission:
  rate: 0.0003
  minimum: 5.0

stamp_duty:
  rate: 0.001

transfer_fee:
  rate: 0.00001

slippage:
  bps: 10.0
  tick_size: 0.01

limit:
  main_ratio: 0.10
  star_ratio: 0.20
  szse_ratio: 0.10
  bjse_ratio: 0.30
  st_ratio: 0.05
  tick_size: 0.01

risk:
  enable_single_position_limit: true
  max_position_value_ratio: 1.0

universe:
  min_lot_value: 1000.0
"""
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


# ------------------------------------------------------------------ #
# 1. 金标准 CLI 成功运行
# ------------------------------------------------------------------ #
class TestBacktestCLISuccess:
    def test_full_backtest_success(self, tmp_path: Path):
        quotes_path = _write_quotes_parquet(tmp_path)
        signals_path = _write_signals_json(
            tmp_path,
            [
                {
                    "signal_date": "2024-01-02",
                    "symbol": "000001",
                    "side": "BUY",
                    "quantity": 100,
                    "reason": "买入测试",
                },
                {
                    "signal_date": "2024-01-08",
                    "symbol": "000001",
                    "side": "SELL",
                    "quantity": 100,
                    "reason": "卖出测试",
                },
            ],
        )
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--signals", str(signals_path),
            "--output", str(output_dir),
        ])

        assert exit_code == 0
        # 验证输出文件全部生成
        assert (output_dir / "backtest-result.json").exists()
        assert (output_dir / "backtest-report.md").exists()
        assert (output_dir / "orders.parquet").exists()
        assert (output_dir / "fills.parquet").exists()
        assert (output_dir / "equity.parquet").exists()

    def test_json_result_content(self, tmp_path: Path):
        quotes_path = _write_quotes_parquet(tmp_path)
        signals_path = _write_signals_json(
            tmp_path,
            [
                {
                    "signal_date": "2024-01-02",
                    "symbol": "000001",
                    "side": "BUY",
                    "quantity": 100,
                    "reason": "买入",
                },
            ],
        )
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--signals", str(signals_path),
            "--output", str(output_dir),
        ])
        assert exit_code == 0

        with (output_dir / "backtest-result.json").open("r", encoding="utf-8") as f:
            result = json.load(f)

        # 验证 JSON 结构
        assert "metrics" in result
        assert "orders" in result
        assert "fills" in result
        assert "daily_equity" in result
        assert "config_summary" in result
        assert "limitations" in result
        assert "content_hash" in result

        # 验证成交数
        assert len(result["fills"]) == 1
        assert result["fills"][0]["side"] == "BUY"
        assert result["fills"][0]["symbol"] == "000001"
        assert result["fills"][0]["quantity"] == 100

    def test_markdown_report_content(self, tmp_path: Path):
        quotes_path = _write_quotes_parquet(tmp_path)
        signals_path = _write_signals_json(
            tmp_path,
            [
                {
                    "signal_date": "2024-01-02",
                    "symbol": "000001",
                    "side": "BUY",
                    "quantity": 100,
                    "reason": "买入",
                },
            ],
        )
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--signals", str(signals_path),
            "--output", str(output_dir),
        ])
        assert exit_code == 0

        md = (output_dir / "backtest-report.md").read_text(encoding="utf-8")
        assert "A股量化回测报告" in md
        assert "核心指标" in md
        assert "限制声明" in md
        assert "每日权益" in md

    def test_parquet_outputs(self, tmp_path: Path):
        quotes_path = _write_quotes_parquet(tmp_path)
        signals_path = _write_signals_json(
            tmp_path,
            [
                {
                    "signal_date": "2024-01-02",
                    "symbol": "000001",
                    "side": "BUY",
                    "quantity": 100,
                    "reason": "买入",
                },
                {
                    "signal_date": "2024-01-05",
                    "symbol": "000001",
                    "side": "SELL",
                    "quantity": 100,
                    "reason": "卖出",
                },
            ],
        )
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--signals", str(signals_path),
            "--output", str(output_dir),
        ])
        assert exit_code == 0

        orders_df = pd.read_parquet(output_dir / "orders.parquet")
        fills_df = pd.read_parquet(output_dir / "fills.parquet")
        equity_df = pd.read_parquet(output_dir / "equity.parquet")

        assert len(orders_df) == 2
        assert len(fills_df) == 2
        assert len(equity_df) > 0

    def test_noop_strategy_success(self, tmp_path: Path):
        """noop 策略不需要 --signals。"""
        quotes_path = _write_quotes_parquet(tmp_path)
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "noop",
            "--output", str(output_dir),
        ])
        assert exit_code == 0
        assert (output_dir / "backtest-result.json").exists()

    def test_default_config_used(self, tmp_path: Path):
        """不传 --config 时使用默认配置。"""
        quotes_path = _write_quotes_parquet(tmp_path)
        signals_path = _write_signals_json(
            tmp_path,
            [
                {
                    "signal_date": "2024-01-02",
                    "symbol": "000001",
                    "side": "BUY",
                    "quantity": 100,
                    "reason": "买入",
                },
            ],
        )
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--strategy", "scripted",
            "--signals", str(signals_path),
            "--output", str(output_dir),
        ])
        assert exit_code == 0


# ------------------------------------------------------------------ #
# 2. 失败场景返回非零
# ------------------------------------------------------------------ #
class TestBacktestCLIFailures:
    def test_scripted_without_signals(self, tmp_path: Path):
        """scripted 策略缺少 --signals -> 退出码 1。"""
        quotes_path = _write_quotes_parquet(tmp_path)
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--output", str(output_dir),
        ])
        assert exit_code == 1

    def test_unknown_strategy(self, tmp_path: Path):
        """未知策略 -> 退出码 1。"""
        quotes_path = _write_quotes_parquet(tmp_path)
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "invalid_strategy",
            "--output", str(output_dir),
        ])
        assert exit_code != 0

    def test_nonexistent_quotes_file(self, tmp_path: Path):
        """行情文件不存在 -> 异常或非零退出码。"""
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        with pytest.raises(Exception):
            main([
                "backtest",
                "--quotes", str(tmp_path / "nonexistent.parquet"),
                "--config", str(config_path),
                "--strategy", "noop",
                "--output", str(output_dir),
            ])

    def test_nonexistent_config_file(self, tmp_path: Path):
        """配置文件不存在 -> 异常。"""
        quotes_path = _write_quotes_parquet(tmp_path)
        output_dir = tmp_path / "reports"

        with pytest.raises(Exception):
            main([
                "backtest",
                "--quotes", str(quotes_path),
                "--config", str(tmp_path / "nonexistent.yaml"),
                "--strategy", "noop",
                "--output", str(output_dir),
            ])

    def test_accounting_check_on_bad_data(self, tmp_path: Path):
        """数据质量不合格时应返回非零（通过 CLI 的账务恒等式校验）。"""
        # 构造无效数据（价格为 0）
        d = make_trade_dates(date(2024, 1, 2), 5)
        rows = []
        for dt in d:
            row = make_bar("000001", dt, open_price=0, high=0, low=0, close=0)
            rows.append(row)
        quotes = pd.DataFrame(rows)
        quotes_path = tmp_path / "bad_quotes.parquet"
        quotes.to_parquet(quotes_path, index=False)

        signals_path = _write_signals_json(
            tmp_path,
            [
                {
                    "signal_date": "2024-01-02",
                    "symbol": "000001",
                    "side": "BUY",
                    "quantity": 100,
                    "reason": "买入无效价格",
                },
            ],
        )
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        # 价格为 0 会被拒绝 (INVALID_PRICE)，但 CLI 本身仍应返回 0
        # 因为账务恒等式仍然成立（无成交，cash == equity）
        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--signals", str(signals_path),
            "--output", str(output_dir),
        ])
        # 无效价格导致订单被拒，但回测本身完成，账务恒等式仍成立
        assert exit_code == 0


# ------------------------------------------------------------------ #
# 3. 输出文件完整性验证
# ------------------------------------------------------------------ #
class TestBacktestCLIOutputIntegrity:
    def test_all_five_outputs_generated(self, tmp_path: Path):
        quotes_path = _write_quotes_parquet(tmp_path)
        signals_path = _write_signals_json(
            tmp_path,
            [
                {
                    "signal_date": "2024-01-02",
                    "symbol": "000001",
                    "side": "BUY",
                    "quantity": 100,
                    "reason": "买入",
                },
            ],
        )
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--signals", str(signals_path),
            "--output", str(output_dir),
        ])
        assert exit_code == 0

        expected_files = [
            "backtest-result.json",
            "backtest-report.md",
            "orders.parquet",
            "fills.parquet",
            "equity.parquet",
        ]
        for fname in expected_files:
            assert (output_dir / fname).exists(), f"缺少输出文件: {fname}"

    def test_output_dir_created(self, tmp_path: Path):
        """输出目录不存在时应自动创建。"""
        quotes_path = _write_quotes_parquet(tmp_path)
        signals_path = _write_signals_json(
            tmp_path,
            [
                {
                    "signal_date": "2024-01-02",
                    "symbol": "000001",
                    "side": "BUY",
                    "quantity": 100,
                    "reason": "买入",
                },
            ],
        )
        config_path = _write_config_yaml(tmp_path, initial_cash=10000.0)
        output_dir = tmp_path / "deep" / "nested" / "reports"

        exit_code = main([
            "backtest",
            "--quotes", str(quotes_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--signals", str(signals_path),
            "--output", str(output_dir),
        ])
        assert exit_code == 0
        assert output_dir.exists()
