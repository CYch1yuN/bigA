"""Phase 3 研究报告生成：JSON、Markdown、Parquet。

输出到 ``reports/phase-3/``，至少包含：

- ``research-summary.json``：完整研究报告（指标、折、参数选择、资格判定等）。
- ``research-report.md``：人类可读 Markdown 报告。
- ``walk-forward-folds.parquet``：滚动折日期边界与各折选定参数。
- ``steady-oos-equity.parquet``：稳健轨拼接样本外权益。
- ``aggressive-oos-equity.parquet``：激进轨拼接样本外权益。
- ``orders.parquet``：全部轨道全部折的订单流水。
- ``fills.parquet``：全部轨道全部折的成交流水。
- ``parameter-results.parquet``：参数扰动结果。
- ``stress-results.parquet``：压力测试结果。
- ``market-regimes.parquet``：市场阶段分类。
- ``monte-carlo-summary.json``：蒙特卡洛概率分析摘要。

报告必须记录：数据文件哈希、配置哈希、代码提交号、全部限制、候选参数全集、
选择过程、随机种子和内容哈希。相同数据、配置和代码提交必须产生字节级一致
的 JSON/Markdown 和内容一致的 Parquet。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from ..manifest import get_code_commit
from ..storage import file_sha256
from .analysis import (
    EligibilityCheck,
    FoldResult,
    ResearchResult,
    TrackResult,
    TrackType,
)
from .monte_carlo import MonteCarloResult
from .stress import MarketRegime, MarketRegimeResult, StressResult
from .walk_forward import Fold

__all__ = [
    "ResearchReportGenerator",
    "compute_config_hash",
    "compute_data_hash",
]


# ---------------------------------------------------------------------- #
# 哈希辅助
# ---------------------------------------------------------------------- #


def compute_config_hash(config_dict: dict[str, Any]) -> str:
    """计算配置字典的确定性 SHA-256 哈希。

    Args:
        config_dict: 配置字典（已转换为 JSON 可序列化类型）。

    Returns:
        64 字符十六进制哈希字符串。
    """
    canonical = json.dumps(config_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_data_hash(file_paths: list[str | Path]) -> str:
    """计算多个数据文件的组合 SHA-256 哈希。

    Args:
        file_paths: 数据文件路径列表。

    Returns:
        64 字符十六进制哈希字符串。
    """
    h = hashlib.sha256()
    for fp in sorted(str(p) for p in file_paths):
        h.update(fp.encode("utf-8"))
        h.update(b"\x00")
        try:
            file_h = file_sha256(fp)
            h.update(file_h.encode("utf-8"))
        except (FileNotFoundError, OSError):
            h.update(b"missing")
        h.update(b"\x00")
    return h.hexdigest()


# ---------------------------------------------------------------------- #
# 报告生成器
# ---------------------------------------------------------------------- #


class ResearchReportGenerator:
    """Phase 3 研究报告生成器。

    生成 JSON、Markdown 和 Parquet 报告，确保相同输入下字节级一致。

    使用方法::

        gen = ResearchReportGenerator()
        gen.generate_all(result, output_dir, config_dict, data_files)
    """

    def generate_all(
        self,
        result: ResearchResult,
        output_dir: str | Path,
        config_dict: Optional[dict[str, Any]] = None,
        data_files: Optional[list[str | Path]] = None,
        initial_cash: float = 1000.0,
    ) -> dict[str, Path]:
        """生成全部报告文件。

        Args:
            result: 研究结果。
            output_dir: 输出目录。
            config_dict: 配置字典（用于哈希）。
            data_files: 数据文件路径列表（用于哈希）。
            initial_cash: 初始资金。

        Returns:
            文件名 -> 路径的字典。
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # 计算哈希
        config_hash = compute_config_hash(config_dict or {})
        data_hash = compute_data_hash(data_files or [])
        code_commit = result.code_commit or get_code_commit()

        # 更新结果中的哈希
        result.config_hash = config_hash
        result.data_hash = data_hash
        result.code_commit = code_commit

        paths: dict[str, Path] = {}

        # 1. JSON 摘要
        summary = self.generate_json(result, initial_cash)
        json_path = out / "research-summary.json"
        json_text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
        json_path.write_text(json_text, encoding="utf-8")
        paths["research-summary.json"] = json_path

        # 2. Markdown 报告
        md_text = self.generate_markdown(result, initial_cash)
        md_path = out / "research-report.md"
        md_path.write_text(md_text, encoding="utf-8")
        paths["research-report.md"] = md_path

        # 3. Parquet 文件
        folds_df = self.generate_folds_dataframe(result)
        folds_path = out / "walk-forward-folds.parquet"
        folds_df.to_parquet(folds_path, index=False)
        paths["walk-forward-folds.parquet"] = folds_path

        steady_equity_df = self.generate_equity_dataframe(result.steady.oos_equity)
        steady_path = out / "steady-oos-equity.parquet"
        steady_equity_df.to_parquet(steady_path, index=False)
        paths["steady-oos-equity.parquet"] = steady_path

        aggressive_equity_df = self.generate_equity_dataframe(result.aggressive.oos_equity)
        aggressive_path = out / "aggressive-oos-equity.parquet"
        aggressive_equity_df.to_parquet(aggressive_path, index=False)
        paths["aggressive-oos-equity.parquet"] = aggressive_path

        orders_df = self.generate_orders_dataframe(result)
        orders_path = out / "orders.parquet"
        orders_df.to_parquet(orders_path, index=False)
        paths["orders.parquet"] = orders_path

        fills_df = self.generate_fills_dataframe(result)
        fills_path = out / "fills.parquet"
        fills_df.to_parquet(fills_path, index=False)
        paths["fills.parquet"] = fills_path

        param_df = self.generate_parameter_results_dataframe(result)
        param_path = out / "parameter-results.parquet"
        param_df.to_parquet(param_path, index=False)
        paths["parameter-results.parquet"] = param_path

        stress_df = self.generate_stress_results_dataframe(result)
        stress_path = out / "stress-results.parquet"
        stress_df.to_parquet(stress_path, index=False)
        paths["stress-results.parquet"] = stress_path

        regime_df = self.generate_market_regimes_dataframe(result)
        regime_path = out / "market-regimes.parquet"
        regime_df.to_parquet(regime_path, index=False)
        paths["market-regimes.parquet"] = regime_path

        # 4. 蒙特卡洛摘要
        mc_summary = self.generate_monte_carlo_json(result)
        mc_path = out / "monte-carlo-summary.json"
        mc_text = json.dumps(mc_summary, ensure_ascii=False, indent=2, default=str)
        mc_path.write_text(mc_text, encoding="utf-8")
        paths["monte-carlo-summary.json"] = mc_path

        return paths

    # ------------------------------------------------------------------ #
    # JSON 报告
    # ------------------------------------------------------------------ #

    def generate_json(
        self, result: ResearchResult, initial_cash: float
    ) -> dict[str, Any]:
        """生成完整 JSON 研究报告字典。"""
        return {
            "metadata": {
                "code_commit": result.code_commit,
                "config_hash": result.config_hash,
                "data_hash": result.data_hash,
                "insufficient_sample": result.insufficient_sample,
                "initial_cash": initial_cash,
                "generated_at": datetime.utcnow().isoformat() + "Z",
            },
            "folds": [self._fold_to_dict(f) for f in result.folds],
            "steady": self._track_to_dict(result.steady),
            "aggressive": self._track_to_dict(result.aggressive),
            "limitations": list(result.limitations),
            "steady_eligibility": self._eligibility_to_dict(result.steady.eligibility),
            "aggressive_eligibility": self._eligibility_to_dict(result.aggressive.eligibility),
        }

    # ------------------------------------------------------------------ #
    # Markdown 报告
    # ------------------------------------------------------------------ #

    def generate_markdown(
        self, result: ResearchResult, initial_cash: float
    ) -> str:
        """生成 Markdown 研究报告。"""
        lines: list[str] = []

        lines.append("# A股双轨策略研究报告（Phase 3）")
        lines.append("")

        # 元数据
        lines.append("## 元数据")
        lines.append("")
        lines.append(f"- 代码提交号: `{result.code_commit or 'no-git'}`")
        lines.append(f"- 配置哈希: `{result.config_hash or 'N/A'}`")
        lines.append(f"- 数据哈希: `{result.data_hash or 'N/A'}`")
        lines.append(f"- 初始资金: {initial_cash:.2f}")
        lines.append(f"- 样本不足: {'是' if result.insufficient_sample else '否'}")
        lines.append("")

        # 滚动折
        lines.append("## 滚动折")
        lines.append("")
        if result.folds:
            lines.append("| 折ID | 训练期 | 验证期 | 测试期 |")
            lines.append("| --- | --- | --- | --- |")
            for f in result.folds:
                lines.append(
                    f"| {f.fold_id} "
                    f"| {f.train_start.isoformat()} ~ {f.train_end.isoformat()} "
                    f"| {f.validation_start.isoformat()} ~ {f.validation_end.isoformat()} "
                    f"| {f.test_start.isoformat()} ~ {f.test_end.isoformat()} |"
                )
        else:
            lines.append("无滚动折（样本不足）。")
        lines.append("")

        # 稳健轨
        lines.append("## 稳健轨")
        lines.append("")
        self._append_track_markdown(lines, result.steady)
        lines.append("")

        # 激进轨
        lines.append("## 激进轨")
        lines.append("")
        self._append_track_markdown(lines, result.aggressive)
        lines.append("")

        # 蒙特卡洛
        if result.aggressive.monte_carlo is not None:
            lines.append("## 蒙特卡洛概率分析（激进轨）")
            lines.append("")
            mc = result.aggressive.monte_carlo
            lines.append(f"- 随机种子: {mc.random_seed}")
            lines.append(f"- 路径数: {mc.n_paths}")
            lines.append(f"- 块长度: {mc.block_length}")
            lines.append(f"- 样本外天数: {mc.n_oos_days}")
            lines.append(f"- 样本不足: {'是' if mc.insufficient_sample else '否'}")
            lines.append(f"- 达到十倍概率: {mc.prob_ten_x:.6f}")
            lines.append(f"- 损失50%概率: {mc.prob_loss_50:.6f}")
            lines.append(f"- 近似归零概率: {mc.prob_near_zero:.6f}")
            lines.append("")
            lines.append("| 分位数 | 期末资金 |")
            lines.append("| --- | --- |")
            for pct, val in sorted(mc.percentiles.items()):
                lines.append(f"| {pct} | {val:.2f} |")
            lines.append("")
            lines.append(
                "> **声明**：蒙特卡洛结果仅用于概率研究，不构成收益承诺。"
                "激进轨永远为 SIMULATION_ONLY。"
            )
            lines.append("")

        # 资格判定
        lines.append("## 资格判定")
        lines.append("")
        lines.append("### 稳健轨")
        lines.append("")
        self._append_eligibility_markdown(lines, result.steady.eligibility)
        lines.append("")
        lines.append("### 激进轨")
        lines.append("")
        self._append_eligibility_markdown(lines, result.aggressive.eligibility)
        lines.append("")

        # 限制声明
        lines.append("## 限制声明")
        lines.append("")
        for item in result.limitations:
            lines.append(f"- {item}")
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Parquet DataFrame 生成
    # ------------------------------------------------------------------ #

    def generate_folds_dataframe(
        self, result: ResearchResult
    ) -> pd.DataFrame:
        """生成滚动折 DataFrame。"""
        columns = [
            "fold_id",
            "train_start",
            "train_end",
            "validation_start",
            "validation_end",
            "test_start",
            "test_end",
            "track",
            "selected_params",
            "selection_reason",
            "eliminated_count",
            "test_total_return",
            "test_max_drawdown",
            "benchmark_hs300",
            "benchmark_csi_all",
        ]
        rows: list[dict[str, Any]] = []
        for track in [result.steady, result.aggressive]:
            for fr in track.folds:
                test_metrics = {}
                if fr.test_result and fr.test_result.metrics:
                    test_metrics = fr.test_result.metrics
                rows.append({
                    "fold_id": fr.fold.fold_id,
                    "train_start": fr.fold.train_start,
                    "train_end": fr.fold.train_end,
                    "validation_start": fr.fold.validation_start,
                    "validation_end": fr.fold.validation_end,
                    "test_start": fr.fold.test_start,
                    "test_end": fr.fold.test_end,
                    "track": track.track_type,
                    "selected_params": json.dumps(fr.selected_params, sort_keys=True, default=str),
                    "selection_reason": fr.selection_reason,
                    "eliminated_count": len(fr.eliminated_candidates),
                    "test_total_return": float(test_metrics.get("total_return", 0.0) or 0.0),
                    "test_max_drawdown": float(test_metrics.get("max_drawdown", 0.0) or 0.0),
                    "benchmark_hs300": float(fr.benchmark_returns.get("hs300", 0.0) or 0.0) if isinstance(fr.benchmark_returns.get("hs300"), (int, float)) else 0.0,
                    "benchmark_csi_all": float(fr.benchmark_returns.get("csi_all", 0.0) or 0.0) if isinstance(fr.benchmark_returns.get("csi_all"), (int, float)) else 0.0,
                })
        return pd.DataFrame(rows, columns=columns)

    def generate_equity_dataframe(
        self, equity: list[Any]
    ) -> pd.DataFrame:
        """生成权益序列 DataFrame。"""
        columns = [
            "snapshot_date",
            "cash",
            "position_value",
            "total_equity",
            "daily_pnl",
            "cumulative_pnl",
            "drawdown",
        ]
        rows: list[dict[str, Any]] = []
        for snap in equity:
            rows.append({
                "snapshot_date": snap.snapshot_date,
                "cash": float(snap.cash),
                "position_value": float(snap.position_value),
                "total_equity": float(snap.total_equity),
                "daily_pnl": float(snap.daily_pnl),
                "cumulative_pnl": float(snap.cumulative_pnl),
                "drawdown": float(snap.drawdown),
            })
        return pd.DataFrame(rows, columns=columns)

    def generate_orders_dataframe(
        self, result: ResearchResult
    ) -> pd.DataFrame:
        """生成全部轨道订单流水 DataFrame。"""
        columns = [
            "track",
            "fold_id",
            "order_id",
            "signal_date",
            "symbol",
            "side",
            "quantity",
            "reason",
            "planned_fill_date",
            "status",
            "reject_reason",
            "reject_detail",
            "filled",
            "audit_flags",
        ]
        rows: list[dict[str, Any]] = []
        for track in [result.steady, result.aggressive]:
            for fr in track.folds:
                if fr.test_result is None:
                    continue
                for o in fr.test_result.orders:
                    sig = o.signal
                    rows.append({
                        "track": track.track_type,
                        "fold_id": fr.fold.fold_id,
                        "order_id": o.order_id,
                        "signal_date": sig.signal_date,
                        "symbol": sig.symbol,
                        "side": self._enum_value(sig.side),
                        "quantity": sig.quantity,
                        "reason": sig.reason,
                        "planned_fill_date": o.planned_fill_date,
                        "status": self._enum_value(o.status),
                        "reject_reason": self._enum_value(o.reject_reason) if o.reject_reason else None,
                        "reject_detail": o.reject_detail,
                        "filled": o.fill is not None,
                        "audit_flags": list(o.audit_flags),
                    })
        return pd.DataFrame(rows, columns=columns)

    def generate_fills_dataframe(
        self, result: ResearchResult
    ) -> pd.DataFrame:
        """生成全部轨道成交流水 DataFrame。"""
        columns = [
            "track",
            "fold_id",
            "order_id",
            "fill_date",
            "symbol",
            "side",
            "quantity",
            "raw_open_price",
            "slippage_price",
            "commission",
            "stamp_duty",
            "transfer_fee",
            "total_cost",
            "cash_change",
            "turnover",
            "audit_flags",
        ]
        rows: list[dict[str, Any]] = []
        for track in [result.steady, result.aggressive]:
            for fr in track.folds:
                if fr.test_result is None:
                    continue
                for f in fr.test_result.fills:
                    rows.append({
                        "track": track.track_type,
                        "fold_id": fr.fold.fold_id,
                        "order_id": f.order_id,
                        "fill_date": f.fill_date,
                        "symbol": f.symbol,
                        "side": self._enum_value(f.side),
                        "quantity": f.quantity,
                        "raw_open_price": float(f.raw_open_price),
                        "slippage_price": float(f.slippage_price),
                        "commission": float(f.commission),
                        "stamp_duty": float(f.stamp_duty),
                        "transfer_fee": float(f.transfer_fee),
                        "total_cost": float(f.total_cost),
                        "cash_change": float(f.cash_change),
                        "turnover": float(f.turnover),
                        "audit_flags": list(f.audit_flags),
                    })
        return pd.DataFrame(rows, columns=columns)

    def generate_parameter_results_dataframe(
        self, result: ResearchResult
    ) -> pd.DataFrame:
        """生成参数扰动结果 DataFrame。"""
        columns = [
            "track",
            "param_key",
            "total_return",
            "annualized_return",
            "max_drawdown",
            "turnover_rate",
            "is_baseline",
        ]
        rows: list[dict[str, Any]] = []

        for track in [result.steady, result.aggressive]:
            pp = track.parameter_perturbation
            if pp is None:
                continue
            for combo in pp.per_combination:
                rows.append({
                    "track": track.track_type,
                    "param_key": combo.get("param_key", ""),
                    "total_return": float(combo.get("total_return", 0.0) or 0.0),
                    "annualized_return": float(combo.get("annualized_return", 0.0) or 0.0),
                    "max_drawdown": float(combo.get("max_drawdown", 0.0) or 0.0),
                    "turnover_rate": float(combo.get("turnover_rate", 0.0) or 0.0),
                    "is_baseline": combo.get("param_key", "") == combo.get("baseline_key", ""),
                })

        return pd.DataFrame(rows, columns=columns)

    def generate_stress_results_dataframe(
        self, result: ResearchResult
    ) -> pd.DataFrame:
        """生成压力测试结果 DataFrame。"""
        columns = [
            "track",
            "scenario_name",
            "fee_multiplier",
            "slippage_multiplier",
            "total_return",
            "annualized_return",
            "max_drawdown",
            "sharpe",
            "calmar",
            "win_rate",
            "turnover_rate",
            "total_trades",
        ]
        rows: list[dict[str, Any]] = []

        for track in [result.steady, result.aggressive]:
            for sr in track.stress_results:
                rows.append({
                    "track": track.track_type,
                    "scenario_name": sr.scenario.name,
                    "fee_multiplier": sr.scenario.fee_multiplier,
                    "slippage_multiplier": sr.scenario.slippage_multiplier,
                    "total_return": sr.total_return,
                    "annualized_return": sr.annualized_return,
                    "max_drawdown": sr.max_drawdown,
                    "sharpe": sr.sharpe,
                    "calmar": sr.calmar,
                    "win_rate": sr.win_rate,
                    "turnover_rate": sr.turnover_rate,
                    "total_trades": sr.total_trades,
                })

        return pd.DataFrame(rows, columns=columns)

    def generate_market_regimes_dataframe(
        self, result: ResearchResult
    ) -> pd.DataFrame:
        """生成市场阶段分类 DataFrame。"""
        columns = [
            "date",
            "regime",
            "hs300_close",
            "hs300_ma120",
            "realized_vol_20",
            "is_bull",
            "is_bear",
            "is_high_volatility",
        ]
        rows: list[dict[str, Any]] = []

        # 从稳健轨或激进轨获取市场阶段
        for track in [result.steady, result.aggressive]:
            if track.market_regime is None:
                continue
            for r in track.market_regime.regimes:
                rows.append({
                    "date": r.date,
                    "regime": r.regime,
                    "hs300_close": r.hs300_close,
                    "hs300_ma120": r.hs300_ma120,
                    "realized_vol_20": r.realized_vol_20,
                    "is_bull": r.is_bull,
                    "is_bear": r.is_bear,
                    "is_high_volatility": r.is_high_volatility,
                })
            break  # 两个轨道使用相同的基准，只输出一次

        return pd.DataFrame(rows, columns=columns)

    # ------------------------------------------------------------------ #
    # 蒙特卡洛 JSON
    # ------------------------------------------------------------------ #

    def generate_monte_carlo_json(
        self, result: ResearchResult
    ) -> dict[str, Any]:
        """生成蒙特卡洛摘要 JSON 字典。"""
        mc = result.aggressive.monte_carlo
        if mc is None:
            return {
                "available": False,
                "reason": "激进轨无样本外权益，未执行蒙特卡洛分析",
            }
        return {
            "available": True,
            "prob_ten_x": mc.prob_ten_x,
            "prob_loss_50": mc.prob_loss_50,
            "prob_near_zero": mc.prob_near_zero,
            "percentiles": dict(mc.percentiles),
            "n_oos_days": mc.n_oos_days,
            "block_length": mc.block_length,
            "n_paths": mc.n_paths,
            "random_seed": mc.random_seed,
            "insufficient_sample": mc.insufficient_sample,
            "disclaimer": (
                "蒙特卡洛结果仅用于概率研究，不构成收益承诺。"
                "激进轨永远为 SIMULATION_ONLY。"
            ),
        }

    # ------------------------------------------------------------------ #
    # 内部序列化辅助
    # ------------------------------------------------------------------ #

    def _fold_to_dict(self, fold: Fold) -> dict[str, Any]:
        """将 Fold 转为 JSON 字典。"""
        return {
            "fold_id": fold.fold_id,
            "train_start": fold.train_start.isoformat(),
            "train_end": fold.train_end.isoformat(),
            "validation_start": fold.validation_start.isoformat(),
            "validation_end": fold.validation_end.isoformat(),
            "test_start": fold.test_start.isoformat(),
            "test_end": fold.test_end.isoformat(),
        }

    def _track_to_dict(self, track: TrackResult) -> dict[str, Any]:
        """将 TrackResult 转为 JSON 字典。"""
        return {
            "track_type": track.track_type,
            "insufficient_sample": track.insufficient_sample,
            "oos_metrics": self._to_jsonable(track.oos_metrics),
            "benchmark_comparison": self._to_jsonable(track.benchmark_comparison),
            "folds": [self._fold_result_to_dict(fr) for fr in track.folds],
            "stress_results": [self._stress_result_to_dict(sr) for sr in track.stress_results],
            "parameter_perturbation": self._perturbation_to_dict(track.parameter_perturbation),
            "market_regime": self._regime_to_dict(track.market_regime),
            "monte_carlo": self._monte_carlo_to_dict(track.monte_carlo),
            "eligibility": self._eligibility_to_dict(track.eligibility),
        }

    def _fold_result_to_dict(self, fr: FoldResult) -> dict[str, Any]:
        """将 FoldResult 转为 JSON 字典。"""
        test_metrics: dict[str, Any] = {}
        if fr.test_result and fr.test_result.metrics:
            test_metrics = self._to_jsonable(fr.test_result.metrics)
        return {
            "fold_id": fr.fold.fold_id,
            "selected_params": fr.selected_params,
            "selection_reason": fr.selection_reason,
            "eliminated_candidates": fr.eliminated_candidates,
            "test_metrics": test_metrics,
            "benchmark_returns": self._to_jsonable(fr.benchmark_returns),
        }

    def _stress_result_to_dict(self, sr: StressResult) -> dict[str, Any]:
        """将 StressResult 转为 JSON 字典。"""
        return {
            "scenario_name": sr.scenario.name,
            "fee_multiplier": sr.scenario.fee_multiplier,
            "slippage_multiplier": sr.scenario.slippage_multiplier,
            "description": sr.scenario.description,
            "total_return": sr.total_return,
            "annualized_return": sr.annualized_return,
            "max_drawdown": sr.max_drawdown,
            "sharpe": sr.sharpe,
            "calmar": sr.calmar,
            "win_rate": sr.win_rate,
            "turnover_rate": sr.turnover_rate,
            "total_trades": sr.total_trades,
        }

    def _perturbation_to_dict(
        self, pp: Optional[Any]
    ) -> Optional[dict[str, Any]]:
        """将 ParameterPerturbationResult 转为 JSON 字典。"""
        if pp is None:
            return None
        return {
            "total_combinations": pp.total_combinations,
            "positive_return_count": pp.positive_return_count,
            "positive_return_ratio": pp.positive_return_ratio,
            "return_median": pp.return_median,
            "return_p10": pp.return_p10,
            "return_p90": pp.return_p90,
            "max_drawdown_median": pp.max_drawdown_median,
            "max_drawdown_p10": pp.max_drawdown_p10,
            "max_drawdown_p90": pp.max_drawdown_p90,
            "turnover_median": pp.turnover_median,
            "turnover_p10": pp.turnover_p10,
            "turnover_p90": pp.turnover_p90,
            "baseline_return": pp.baseline_return,
            "baseline_max_drawdown": pp.baseline_max_drawdown,
            "baseline_turnover": pp.baseline_turnover,
        }

    def _regime_to_dict(
        self, mr: Optional[MarketRegimeResult]
    ) -> Optional[dict[str, Any]]:
        """将 MarketRegimeResult 转为 JSON 字典。"""
        if mr is None:
            return None
        return {
            "bull_return": mr.bull_return,
            "bull_max_drawdown": mr.bull_max_drawdown,
            "bull_trades": mr.bull_trades,
            "bull_cash_ratio": mr.bull_cash_ratio,
            "bear_return": mr.bear_return,
            "bear_max_drawdown": mr.bear_max_drawdown,
            "bear_trades": mr.bear_trades,
            "bear_cash_ratio": mr.bear_cash_ratio,
            "high_vol_return": mr.high_vol_return,
            "high_vol_max_drawdown": mr.high_vol_max_drawdown,
            "high_vol_trades": mr.high_vol_trades,
            "high_vol_cash_ratio": mr.high_vol_cash_ratio,
            "regime_count": len(mr.regimes),
        }

    def _monte_carlo_to_dict(
        self, mc: Optional[MonteCarloResult]
    ) -> Optional[dict[str, Any]]:
        """将 MonteCarloResult 转为 JSON 字典。"""
        if mc is None:
            return None
        return {
            "prob_ten_x": mc.prob_ten_x,
            "prob_loss_50": mc.prob_loss_50,
            "prob_near_zero": mc.prob_near_zero,
            "percentiles": dict(mc.percentiles),
            "n_oos_days": mc.n_oos_days,
            "block_length": mc.block_length,
            "n_paths": mc.n_paths,
            "random_seed": mc.random_seed,
            "insufficient_sample": mc.insufficient_sample,
        }

    def _eligibility_to_dict(
        self, ec: Optional[EligibilityCheck]
    ) -> Optional[dict[str, Any]]:
        """将 EligibilityCheck 转为 JSON 字典。"""
        if ec is None:
            return None
        return {
            "status": ec.status,
            "conditions": ec.conditions,
            "failure_reasons": ec.failure_reasons,
        }

    # ------------------------------------------------------------------ #
    # Markdown 辅助
    # ------------------------------------------------------------------ #

    def _append_track_markdown(
        self, lines: list[str], track: TrackResult
    ) -> None:
        """追加单条轨道的 Markdown 内容。"""
        m = track.oos_metrics
        lines.append(f"- 轨道类型: {track.track_type}")
        lines.append(f"- 样本不足: {'是' if track.insufficient_sample else '否'}")
        if m:
            lines.append(f"- 总收益: {self._fmt_pct(m.get('total_return', 0.0))}")
            lines.append(f"- 年化收益: {self._fmt_pct(m.get('annualized_return', 0.0))}")
            lines.append(f"- 最大回撤: {self._fmt_pct(m.get('max_drawdown', 0.0))}")
            lines.append(f"- Sharpe: {self._fmt_num(m.get('sharpe', 0.0))}")
            lines.append(f"- Calmar: {self._fmt_num(m.get('calmar', 0.0))}")
            lines.append(f"- 交易天数: {m.get('trading_days', 0)}")
            lines.append(f"- 现金占比: {self._fmt_pct(m.get('cash_ratio', 0.0))}")
        lines.append("")

        # 各折摘要
        if track.folds:
            lines.append("### 各折结果")
            lines.append("")
            lines.append("| 折ID | 选定参数 | 选择理由 | 测试总收益 | 测试回撤 | HS300 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for fr in track.folds:
                test_ret = 0.0
                test_dd = 0.0
                if fr.test_result and fr.test_result.metrics:
                    test_ret = float(fr.test_result.metrics.get("total_return", 0.0) or 0.0)
                    test_dd = float(fr.test_result.metrics.get("max_drawdown", 0.0) or 0.0)
                hs300 = fr.benchmark_returns.get("hs300", 0.0)
                if not isinstance(hs300, (int, float)):
                    hs300 = 0.0
                params_str = json.dumps(fr.selected_params, default=str)
                lines.append(
                    f"| {fr.fold.fold_id} "
                    f"| {params_str} "
                    f"| {fr.selection_reason} "
                    f"| {self._fmt_pct(test_ret)} "
                    f"| {self._fmt_pct(test_dd)} "
                    f"| {self._fmt_pct(hs300)} |"
                )
            lines.append("")

        # 基准比较
        bc = track.benchmark_comparison
        if bc and "error" not in bc:
            lines.append("### 基准比较")
            lines.append("")
            lines.append(f"- 沪深300收益: {self._fmt_pct(bc.get('hs300_return', 0.0))}")
            lines.append(f"- 中证全指收益: {self._fmt_pct(bc.get('csi_all_return', 0.0))}")
            lines.append(f"- 超额(vs HS300): {self._fmt_pct(bc.get('excess_vs_hs300', 0.0))}")
            lines.append(f"- 超额(vs 中证全指): {self._fmt_pct(bc.get('excess_vs_csi_all', 0.0))}")
            lines.append("")

        # 压力测试
        if track.stress_results:
            lines.append("### 压力测试")
            lines.append("")
            lines.append("| 场景 | 费用倍数 | 滑点倍数 | 年化收益 | 最大回撤 | Sharpe |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for sr in track.stress_results:
                lines.append(
                    f"| {sr.scenario.name} "
                    f"| {sr.scenario.fee_multiplier} "
                    f"| {sr.scenario.slippage_multiplier} "
                    f"| {self._fmt_pct(sr.annualized_return)} "
                    f"| {self._fmt_pct(sr.max_drawdown)} "
                    f"| {self._fmt_num(sr.sharpe)} |"
                )
            lines.append("")

        # 参数扰动
        pp = track.parameter_perturbation
        if pp is not None:
            lines.append("### 参数扰动")
            lines.append("")
            lines.append(f"- 总组合数: {pp.total_combinations}")
            lines.append(f"- 正收益比例: {self._fmt_pct(pp.positive_return_ratio)}")
            lines.append(f"- 收益中位数: {self._fmt_pct(pp.return_median)}")
            lines.append(f"- 收益P10: {self._fmt_pct(pp.return_p10)}")
            lines.append(f"- 收益P90: {self._fmt_pct(pp.return_p90)}")
            lines.append(f"- 基线收益: {self._fmt_pct(pp.baseline_return)}")
            lines.append("")

        # 市场阶段
        mr = track.market_regime
        if mr is not None:
            lines.append("### 市场阶段分析")
            lines.append("")
            lines.append("| 阶段 | 总收益 | 最大回撤 | 交易次数 | 现金占比 |")
            lines.append("| --- | --- | --- | --- | --- |")
            lines.append(
                f"| 牛市 | {self._fmt_pct(mr.bull_return)} "
                f"| {self._fmt_pct(mr.bull_max_drawdown)} "
                f"| {mr.bull_trades} "
                f"| {self._fmt_pct(mr.bull_cash_ratio)} |"
            )
            lines.append(
                f"| 熊市 | {self._fmt_pct(mr.bear_return)} "
                f"| {self._fmt_pct(mr.bear_max_drawdown)} "
                f"| {mr.bear_trades} "
                f"| {self._fmt_pct(mr.bear_cash_ratio)} |"
            )
            lines.append(
                f"| 高波动 | {self._fmt_pct(mr.high_vol_return)} "
                f"| {self._fmt_pct(mr.high_vol_max_drawdown)} "
                f"| {mr.high_vol_trades} "
                f"| {self._fmt_pct(mr.high_vol_cash_ratio)} |"
            )
            lines.append("")

    def _append_eligibility_markdown(
        self, lines: list[str], ec: Optional[EligibilityCheck]
    ) -> None:
        """追加资格判定的 Markdown 内容。"""
        if ec is None:
            lines.append("未执行资格判定。")
            return
        lines.append(f"- **状态**: `{ec.status}`")
        if ec.failure_reasons:
            lines.append("- **失败原因**:")
            for r in ec.failure_reasons:
                lines.append(f"  - {r}")
        if ec.conditions:
            lines.append("- **条件检查**:")
            lines.append("")
            lines.append("| 条件 | 值 | 阈值 | 通过 |")
            lines.append("| --- | --- | --- | --- |")
            for c in ec.conditions:
                val = c.get("value", "")
                thr = c.get("threshold", "")
                passed = "是" if c.get("passed") else "否"
                if isinstance(val, float):
                    val = self._fmt_pct(val) if abs(val) <= 1 else self._fmt_num(val)
                if isinstance(thr, float):
                    thr = self._fmt_pct(thr) if abs(thr) <= 1 else str(thr)
                lines.append(f"| {c.get('name', '')} | {val} | {thr} | {passed} |")

    # ------------------------------------------------------------------ #
    # 通用辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_jsonable(obj: Any) -> Any:
        """递归将 Decimal/date/Enum 转为 JSON 可序列化类型。"""
        if obj is None:
            return None
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {str(k): ResearchReportGenerator._to_jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [ResearchReportGenerator._to_jsonable(v) for v in obj]
        return obj

    @staticmethod
    def _enum_value(value: Any) -> Any:
        """安全获取枚举的 value。"""
        if isinstance(value, Enum):
            return value.value
        return value

    @staticmethod
    def _fmt_pct(value: Any) -> str:
        if value is None:
            return "N/A"
        return f"{float(value) * 100:.2f}%"

    @staticmethod
    def _fmt_num(value: Any, places: int = 4) -> str:
        if value is None:
            return "N/A"
        return f"{float(value):.{places}f}"
