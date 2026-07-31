"""FR-07 严格回归测试：跨回测订单 ID 碰撞与空结果哈希。

验证：
1. 相同输入重复运行 ID 一致
2. 000001 与 600000 的订单 ID 不同
3. 相同 symbol 但行情内容不同，ID 不同
4. BUY 与 SELL 不同
5. 100 股与 200 股不同
6. 两条完全相同的重复信号 ID 不同且跨运行稳定
7. Fill.order_id 与对应 Order 一致
8. 两次独立 CLI 进程结果一致
9. 空结果 content_hash 非空且一致
10. 改变配置或日期时空结果哈希变化
11. _compute_hash 不静默返回空字符串
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.models import (
    BacktestResult,
    OrderStatus,
    Side,
    Signal,
    to_decimal,
)
from ashare_quant.backtest.report import ReportGenerator
from ashare_quant.backtest.strategies import ScriptedStrategy
from tests.backtest_samples import make_quotes, make_trade_dates


# ------------------------------------------------------------------ #
# 辅助函数
# ------------------------------------------------------------------ #
def _run(
    data: Optional[pd.DataFrame] = None,
    signals: Optional[list[Signal]] = None,
    config: Optional[BacktestConfig] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
):
    cfg = config or BacktestConfig(initial_cash=100000)
    df = data if data is not None else make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
    sigs = signals if signals is not None else [Signal(date(2024, 1, 2), "000001", Side.BUY, 100)]
    dates = make_trade_dates(date(2024, 1, 2), 10)
    s = start or dates[0]
    e = end or dates[-1]
    engine = BacktestEngine()
    return engine.run(
        data=df,
        strategy=ScriptedStrategy(sigs),
        start_date=s,
        end_date=e,
        initial_cash=cfg.initial_cash,
        config=cfg,
    )


# ------------------------------------------------------------------ #
# 1. 相同输入重复运行 ID 一致
# ------------------------------------------------------------------ #
class TestDeterministicAcrossRuns:
    """相同输入重复运行，所有 order_id 完全一致。"""

    def test_identical_input_same_ids(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "buy"),
            Signal(d[3], "000001", Side.SELL, 100, "sell"),
        ]
        r1 = _run(quotes, signals)
        r2 = _run(quotes, signals)

        ids1 = [o.order_id for o in r1.orders]
        ids2 = [o.order_id for o in r2.orders]
        assert ids1 == ids2, f"order_id 不一致:\n  r1={ids1}\n  r2={ids2}"


# ------------------------------------------------------------------ #
# 2. 000001 与 600000 的订单 ID 不同
# ------------------------------------------------------------------ #
class TestCrossSymbolIdUniqueness:
    """不同 symbol 的订单 ID 必须不同，即使配置、日期、初始资金相同。"""

    def test_different_symbols_different_ids(self):
        quotes_a = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        quotes_b = make_quotes("600000", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)

        # 相同配置、日期、初始资金
        config = BacktestConfig(initial_cash=100000)
        signals_a = [Signal(d[0], "000001", Side.BUY, 100, "buy")]
        signals_b = [Signal(d[0], "600000", Side.BUY, 100, "buy")]

        r1 = _run(quotes_a, signals_a, config)
        r2 = _run(quotes_b, signals_b, config)

        id_a = r1.orders[0].order_id
        id_b = r2.orders[0].order_id

        assert id_a != id_b, (
            f"跨 symbol ID 碰撞: 000001 和 600000 产生相同 order_id={id_a}\n"
            f"  content_hash_a={r1.content_hash}\n"
            f"  content_hash_b={r2.content_hash}"
        )

    def test_different_symbols_all_ids_differ(self):
        """多 symbol 场景下所有 ID 唯一。"""
        d = make_trade_dates(date(2024, 1, 2), 10)
        config = BacktestConfig(initial_cash=100000)
        symbols = ["000001", "000002", "600000", "600001", "300001"]
        all_ids = []
        for sym in symbols:
            quotes = make_quotes(sym, date(2024, 1, 2), 10, base_price=10.0)
            signals = [Signal(d[0], sym, Side.BUY, 100, "buy")]
            result = _run(quotes, signals, config)
            all_ids.extend(o.order_id for o in result.orders)

        assert len(all_ids) == len(set(all_ids)), (
            f"存在跨 symbol ID 碰撞: {all_ids}"
        )


# ------------------------------------------------------------------ #
# 3. 相同 symbol 但行情内容不同，ID 不同
# ------------------------------------------------------------------ #
class TestSameSymbolDifferentData:
    """相同 symbol 但行情内容不同时，订单 ID 必须不同。"""

    def test_different_data_different_ids(self):
        quotes1 = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        quotes2 = make_quotes("000001", date(2024, 1, 2), 10, base_price=20.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "buy")]

        r1 = _run(quotes1, signals)
        r2 = _run(quotes2, signals)

        id1 = r1.orders[0].order_id
        id2 = r2.orders[0].order_id

        assert id1 != id2, (
            f"不同行情数据产生相同 order_id: {id1}\n"
            f"  data1 base_price=10.0\n"
            f"  data2 base_price=20.0"
        )


# ------------------------------------------------------------------ #
# 4. BUY 与 SELL 不同
# ------------------------------------------------------------------ #
class TestSideDifference:
    """BUY 与 SELL 的订单 ID 必须不同。"""

    def test_buy_sell_different_ids(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)

        r_buy = _run(quotes, [Signal(d[0], "000001", Side.BUY, 100, "trade")])
        r_sell = _run(quotes, [Signal(d[0], "000001", Side.SELL, 100, "trade")])

        id_buy = r_buy.orders[0].order_id
        id_sell = r_sell.orders[0].order_id

        assert id_buy != id_sell, (
            f"BUY 和 SELL 产生相同 order_id: {id_buy}"
        )


# ------------------------------------------------------------------ #
# 5. 100 股与 200 股不同
# ------------------------------------------------------------------ #
class TestQuantityDifference:
    """不同数量的订单 ID 必须不同。"""

    def test_different_quantity_different_ids(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)

        r100 = _run(quotes, [Signal(d[0], "000001", Side.BUY, 100, "buy")])
        r200 = _run(quotes, [Signal(d[0], "000001", Side.BUY, 200, "buy")])

        id100 = r100.orders[0].order_id
        id200 = r200.orders[0].order_id

        assert id100 != id200, (
            f"100 股和 200 股产生相同 order_id: {id100}"
        )


# ------------------------------------------------------------------ #
# 6. 两条完全相同的重复信号 ID 不同且跨运行稳定
# ------------------------------------------------------------------ #
class TestDuplicateSignalStability:
    """两条完全相同的重复信号获得不同 ID，且跨运行稳定。"""

    def test_duplicate_signals_unique_and_stable(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "dup"),
            Signal(d[0], "000001", Side.BUY, 100, "dup"),
        ]
        r1 = _run(quotes, signals)
        r2 = _run(quotes, signals)

        ids1 = [o.order_id for o in r1.orders]
        ids2 = [o.order_id for o in r2.orders]

        # 同一次运行内唯一
        assert len(ids1) == 2
        assert len(set(ids1)) == 2, f"重复信号 ID 不唯一: {ids1}"

        # 跨运行稳定
        assert ids1 == ids2, f"重复信号 ID 跨运行不稳定:\n  r1={ids1}\n  r2={ids2}"


# ------------------------------------------------------------------ #
# 7. Fill.order_id 与对应 Order 一致
# ------------------------------------------------------------------ #
class TestFillOrderConsistency:
    """Fill.order_id 必须与对应 Order.order_id 一致。"""

    def test_fill_matches_order(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "buy"),
            Signal(d[3], "000001", Side.SELL, 100, "sell"),
        ]
        result = _run(quotes, signals)

        order_ids = {o.order_id for o in result.orders}
        for fill in result.fills:
            assert fill.order_id in order_ids, (
                f"fill.order_id={fill.order_id} 不在 order_ids={order_ids} 中"
            )


# ------------------------------------------------------------------ #
# 8. 两次独立 CLI 进程结果一致
# ------------------------------------------------------------------ #
class TestCliProcessReproducibility:
    """两次独立 CLI 进程的输出报告完全一致。"""

    def test_two_cli_runs_identical(self, tmp_path):
        repo_root = Path(__file__).resolve().parent.parent
        quotes_path = repo_root / "reports" / "phase-2" / "sample-quotes.parquet"
        signals_path = repo_root / "reports" / "phase-2" / "sample-signals.json"
        config_path = repo_root / "reports" / "phase-2" / "example-config.yaml"

        if not quotes_path.exists():
            pytest.skip("示例行情文件不存在")

        env = {
            "PYTHONPATH": f"{repo_root / 'src'};{repo_root}",
        }
        # 合并当前环境变量
        import os
        env_full = {**os.environ, **env}

        run1_dir = tmp_path / "run1"
        run2_dir = tmp_path / "run2"
        run1_dir.mkdir()
        run2_dir.mkdir()

        cmd = [
            sys.executable, "-c",
            "from ashare_quant.cli import main; main()",
            "backtest",
            "--quotes", str(quotes_path),
            "--signals", str(signals_path),
            "--config", str(config_path),
            "--strategy", "scripted",
            "--output",
        ]

        subprocess.run(cmd + [str(run1_dir)], check=True, env=env_full,
                       capture_output=True, cwd=str(repo_root))
        subprocess.run(cmd + [str(run2_dir)], check=True, env=env_full,
                       capture_output=True, cwd=str(repo_root))

        # JSON 比较
        json1 = json.loads((run1_dir / "backtest-result.json").read_text(encoding="utf-8"))
        json2 = json.loads((run2_dir / "backtest-result.json").read_text(encoding="utf-8"))
        assert json1 == json2, "两次 CLI 进程 JSON 不一致"

        # order_id 比较
        ids1 = [o["order_id"] for o in json1["orders"]]
        ids2 = [o["order_id"] for o in json2["orders"]]
        assert ids1 == ids2, f"CLI 进程间 order_id 不一致:\n  r1={ids1}\n  r2={ids2}"

        # Markdown 比较
        md1 = (run1_dir / "backtest-report.md").read_text(encoding="utf-8")
        md2 = (run2_dir / "backtest-report.md").read_text(encoding="utf-8")
        assert md1 == md2, "两次 CLI 进程 Markdown 不一致"

        # Parquet 比较
        for fname in ["orders.parquet", "fills.parquet", "equity.parquet"]:
            df1 = pd.read_parquet(run1_dir / fname)
            df2 = pd.read_parquet(run2_dir / fname)
            pd.testing.assert_frame_equal(df1, df2)


# ------------------------------------------------------------------ #
# 9. 空结果 content_hash 非空且一致
# ------------------------------------------------------------------ #
class TestEmptyResultContentHash:
    """空结果必须有 content_hash，且相同输入产生相同哈希。"""

    def test_empty_result_hash_non_empty(self):
        """空结果 content_hash 非空。"""
        config = BacktestConfig(initial_cash=100000)
        engine = BacktestEngine()
        # 使用空 DataFrame 触发空结果路径
        empty_df = pd.DataFrame()
        result = engine.run(
            data=empty_df,
            strategy=ScriptedStrategy([]),
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 15),
            initial_cash=config.initial_cash,
            config=config,
        )
        assert result.content_hash is not None
        assert result.content_hash != ""
        assert len(result.content_hash) == 64  # SHA-256 hex

    def test_empty_result_hash_consistent(self):
        """相同空结果哈希一致。"""
        config = BacktestConfig(initial_cash=100000)
        engine1 = BacktestEngine()
        engine2 = BacktestEngine()
        empty_df = pd.DataFrame()

        r1 = engine1.run(
            data=empty_df, strategy=ScriptedStrategy([]),
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 15),
            initial_cash=config.initial_cash, config=config,
        )
        r2 = engine2.run(
            data=empty_df, strategy=ScriptedStrategy([]),
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 15),
            initial_cash=config.initial_cash, config=config,
        )
        assert r1.content_hash == r2.content_hash

    def test_empty_result_hash_changes_with_config(self):
        """改变配置时空结果哈希变化。"""
        empty_df = pd.DataFrame()
        engine = BacktestEngine()

        r1 = engine.run(
            data=empty_df, strategy=ScriptedStrategy([]),
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 15),
            initial_cash=100000, config=BacktestConfig(initial_cash=100000),
        )
        r2 = engine.run(
            data=empty_df, strategy=ScriptedStrategy([]),
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 15),
            initial_cash=200000, config=BacktestConfig(initial_cash=200000),
        )
        assert r1.content_hash != r2.content_hash, (
            "不同配置的空结果应产生不同 content_hash"
        )

    def test_empty_result_hash_changes_with_dates(self):
        """改变日期时空结果哈希变化。"""
        empty_df = pd.DataFrame()
        config = BacktestConfig(initial_cash=100000)
        engine = BacktestEngine()

        r1 = engine.run(
            data=empty_df, strategy=ScriptedStrategy([]),
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 15),
            initial_cash=config.initial_cash, config=config,
        )
        r2 = engine.run(
            data=empty_df, strategy=ScriptedStrategy([]),
            start_date=date(2024, 2, 1), end_date=date(2024, 2, 28),
            initial_cash=config.initial_cash, config=config,
        )
        assert r1.content_hash != r2.content_hash, (
            "不同日期的空结果应产生不同 content_hash"
        )


# ------------------------------------------------------------------ #
# 10. _compute_hash 不静默返回空字符串
# ------------------------------------------------------------------ #
class TestComputeHashNoSilentFailure:
    """_compute_hash 不应捕获异常后静默返回空字符串。"""

    def test_hash_not_empty_for_valid_result(self):
        """正常结果的 content_hash 非空。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        result = _run(quotes, signals)

        assert result.content_hash is not None
        assert result.content_hash != ""
        assert len(result.content_hash) == 64

    def test_hash_raises_on_invalid_result(self):
        """传入无效结果时 _compute_hash 应抛出异常而非返回空字符串。"""

        class ExplodingObj:
            """str() 调用时抛出异常，确保 json.dumps(default=str) 无法处理。"""
            def __str__(self):
                raise RuntimeError("intentional serialization failure")

        engine = BacktestEngine()
        bad_result = BacktestResult(
            config_summary={},
            orders=[],
            fills=[],
            daily_equity=[],
            final_positions={},
            limitations=[],
            data_range={},
        )
        # 注入 str() 会抛出异常的对象
        bad_result.config_summary = {"bad": ExplodingObj()}

        # 应抛出异常，而非返回空字符串
        with pytest.raises(Exception):
            engine._compute_hash(bad_result)
