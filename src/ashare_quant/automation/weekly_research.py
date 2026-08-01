"""Phase 4 每周研究步骤：复用 Phase 3 ``ResearchRunner`` 跑真实完整周研究。

本模块是周报里的"只读研究"环节。它在本地已有的 curated 行情、历史状态表与基准
数据之上，跑一次 Phase 3 的滚动样本外验证（双轨 / 81-729 参数集 / MC-10000 /
费用压力 / 参数扰动 / 市场阶段分析），把研究结论（**非实盘信号**）汇入周报。

设计要点
--------
* **复用，不重写**：构建 ``ResearchRunner`` 的配方与 ``cmd_research`` 完全一致——
  ``WalkForwardConfig``、``MonteCarloConfig(n_paths=10_000)``、由
  ``STEADY_PARAM_CANDIDATES`` / ``AGGRESSIVE_PARAM_CANDIDATES`` 展开的 81 / 729
  参数组合、``HistoricalUniverseFilter``、``BenchmarkData``、压力与制度分析。
* **可注入**：``runner_factory`` 与 ``data_loader`` 都可注入，便于离线测试用极小
  真实配置或不跑重引擎。
* **优雅降级**：参考研究数据缺失或样本不足时，步骤只记录 skip 原因并继续周报，
  绝不因为"这周没研究成"而把整份周报搞挂。
* **边界**：仅产出研究信号 / 模拟研究结论，``NOT live-trading``。稳健轨结论恒为
  ``NOT_ELIGIBLE_FOR_LIVE_TRADING`` 或 ``INSUFFICIENT_RESEARCH_SAMPLE``。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

import pandas as pd

from ..backtest.config import BacktestConfig
from ..research.analysis import ResearchResult, ResearchRunner
from ..research.benchmarks import BenchmarkData, load_benchmarks
from ..research.monte_carlo import MonteCarloConfig
from ..research.strategies import (
    AGGRESSIVE_PARAM_CANDIDATES,
    STEADY_PARAM_CANDIDATES,
    generate_aggressive_param_combinations,
    generate_steady_param_combinations,
)
from ..research.universe import (
    HistoricalStatusTable,
    HistoricalUniverseFilter,
    load_historical_status,
)
from ..research.walk_forward import WalkForwardConfig

__all__ = [
    "ResearchInputs",
    "ResearchSummary",
    "ResearchDataLoader",
    "LocalResearchDataLoader",
    "build_research_runner",
    "execute_weekly_research",
    "summarize_research",
    "run_weekly_research_step",
    "DEFAULT_WALK_FORWARD_CONFIG",
    "DEFAULT_MONTE_CARLO_CONFIG",
]


# ---------------------------------------------------------------------- #
# 默认研究配置（与 cmd_research 的正统配方一致）
# ---------------------------------------------------------------------- #

#: 滚动切分：3 年训练 + 6 个月验证 + 1 年测试，最少 5 个完整年。
DEFAULT_WALK_FORWARD_CONFIG = WalkForwardConfig(
    train_years=3,
    validation_months=6,
    test_years=1,
    step_years=1,
    min_total_years=5,
)

#: 蒙特卡洛：10,000 条路径，块自助法。
DEFAULT_MONTE_CARLO_CONFIG = MonteCarloConfig(
    random_seed=20260731,
    n_paths=10_000,
    path_length=244,
    block_length=5,
    initial_capital=1000.0,
    ten_x_target=10_000.0,
    loss_50_threshold=500.0,
    near_zero_threshold=100.0,
)

#: 宇宙过滤器默认阈值（与 cmd_research 一致；真实数据缺失时研究步骤会优雅跳过）。
_DEFAULT_UNIVERSE_KWARGS: dict[str, Any] = {
    "min_listing_days": 120,
    "min_valid_days": 15,
    "valid_days_window": 20,
    "min_turnover": 20_000_000.0,
    "turnover_window": 20,
    "lot_size": 100,
    "available_cash": 1000.0,
}


# ---------------------------------------------------------------------- #
# 数据载体
# ---------------------------------------------------------------------- #


class ResearchDataUnavailable(Exception):
    """本地研究参考数据（行情 / 状态表 / 基准）不可用。"""


@dataclass
class ResearchInputs:
    """喂给 ``ResearchRunner`` 的三件套。"""

    quotes: pd.DataFrame
    benchmark: BenchmarkData
    status_table: HistoricalStatusTable


# ---------------------------------------------------------------------- #
# 摘要（可序列化，进入周报与审计产物）
# ---------------------------------------------------------------------- #


@dataclass
class ResearchSummary:
    """研究结论的精简、可序列化摘要。"""

    ran: bool = False
    skipped_reason: Optional[str] = None
    error: Optional[str] = None

    insufficient_sample: bool = False
    folds: int = 0

    steady_eligibility: Optional[str] = None
    aggressive_eligibility: Optional[str] = None

    steady_total_return: Optional[float] = None
    steady_max_drawdown: Optional[float] = None
    steady_sharpe: Optional[float] = None
    aggressive_total_return: Optional[float] = None
    aggressive_max_drawdown: Optional[float] = None
    aggressive_sharpe: Optional[float] = None

    stress: dict[str, Any] = field(default_factory=dict)
    monte_carlo: dict[str, Any] = field(default_factory=dict)
    regime: dict[str, Any] = field(default_factory=dict)
    perturbation: dict[str, Any] = field(default_factory=dict)

    data_hash: Optional[str] = None
    config_hash: Optional[str] = None
    code_commit: Optional[str] = None
    candidate_counts: dict[str, int] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 安全的字典。"""
        return {
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "insufficient_sample": self.insufficient_sample,
            "folds": self.folds,
            "steady_eligibility": self.steady_eligibility,
            "aggressive_eligibility": self.aggressive_eligibility,
            "steady_total_return": self.steady_total_return,
            "steady_max_drawdown": self.steady_max_drawdown,
            "steady_sharpe": self.steady_sharpe,
            "aggressive_total_return": self.aggressive_total_return,
            "aggressive_max_drawdown": self.aggressive_max_drawdown,
            "aggressive_sharpe": self.aggressive_sharpe,
            "stress": self.stress,
            "monte_carlo": self.monte_carlo,
            "regime": self.regime,
            "perturbation": self.perturbation,
            "data_hash": self.data_hash,
            "config_hash": self.config_hash,
            "code_commit": self.code_commit,
            "candidate_counts": self.candidate_counts,
            "limitations": self.limitations,
        }


# ---------------------------------------------------------------------- #
# 数据加载器
# ---------------------------------------------------------------------- #


class ResearchDataLoader(Protocol):
    """把本地参考目录解析成 ``ResearchInputs``（缺失则返回 None）。"""

    def load(self) -> Optional[ResearchInputs]:
        ...


def _hash_dataframe(df: pd.DataFrame) -> str:
    """对 DataFrame 做确定性 SHA-256（按列排序后 CSV 序列化）。"""
    try:
        payload = df.to_csv(index=False).encode("utf-8")
    except Exception:
        payload = repr(sorted(df.columns)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class LocalResearchDataLoader:
    """从本地研究目录读取行情 / 基准 / 历史状态表三件套。

    目录约定（离线、需预先由参考数据采集任务落地，不在本步骤联网获取）：

    * ``quotes.parquet``            —— curated 日行情（研究输入 schema）。
    * ``benchmark.parquet``         —— 含 ``trade_date`` / ``hs300_close`` /
      ``csi_all_close`` 的基准数据。
    * ``security_status.parquet``   —— 历史时点状态表（缺省回退 ``status.parquet``）。

    任意一件缺失都返回 ``None``，交由调用方优雅跳过。
    """

    def __init__(self, research_dir: Path | str) -> None:
        self.research_dir = Path(research_dir)

    def load(self) -> Optional[ResearchInputs]:
        if not self.research_dir.exists():
            return None

        quotes_path = self._first_match("quotes*.parquet")
        bench_path = self.research_dir / "benchmark.parquet"
        status_path = self.research_dir / "security_status.parquet"
        if not status_path.exists():
            status_path = self.research_dir / "status.parquet"

        if quotes_path is None or not bench_path.exists() or not status_path.exists():
            return None

        try:
            quotes = pd.read_parquet(quotes_path)
            benchmark = load_benchmarks(str(bench_path))
            status_table = load_historical_status(str(status_path))
        except Exception as exc:  # noqa: BLE001 - 数据损坏不应炸掉周报
            raise ResearchDataUnavailable(f"读取研究参考数据失败: {exc}") from exc

        if quotes is None or quotes.empty:
            return None
        return ResearchInputs(
            quotes=quotes,
            benchmark=benchmark,
            status_table=status_table,
        )

    def _first_match(self, pattern: str) -> Optional[Path]:
        hits = sorted(self.research_dir.glob(pattern))
        return hits[0] if hits else None


# ---------------------------------------------------------------------- #
# 运行器构建与执行
# ---------------------------------------------------------------------- #


def _resolve_candidates(
    steady: Optional[dict[str, list]],
    aggressive: Optional[dict[str, list]],
) -> tuple[dict[str, list], dict[str, list], int, int]:
    """解析参数候选集（默认使用完整 81 / 729 网格）。"""
    steady = steady if steady is not None else STEADY_PARAM_CANDIDATES
    aggressive = aggressive if aggressive is not None else AGGRESSIVE_PARAM_CANDIDATES
    n_steady = len(generate_steady_param_combinations(steady))
    n_aggr = len(generate_aggressive_param_combinations(aggressive))
    return steady, aggressive, n_steady, n_aggr


def build_research_runner(
    inputs: ResearchInputs,
    *,
    bt_config: Optional[BacktestConfig] = None,
    wf_config: Optional[WalkForwardConfig] = None,
    mc_config: Optional[MonteCarloConfig] = None,
    steady_candidates: Optional[dict[str, list]] = None,
    aggressive_candidates: Optional[dict[str, list]] = None,
    universe_kwargs: Optional[dict[str, Any]] = None,
) -> ResearchRunner:
    """复用 Phase 3 正统配方，构建真实 ``ResearchRunner``。

    默认即完整 81-729 参数集 + MC-10000；``steady_candidates`` /
    ``aggressive_candidates`` 传 None 时使用代码内置默认网格。
    """
    bt_config = bt_config or BacktestConfig(initial_cash=1000.0)
    wf_config = wf_config or DEFAULT_WALK_FORWARD_CONFIG
    mc_config = mc_config or DEFAULT_MONTE_CARLO_CONFIG

    steady_candidates, aggressive_candidates, _, _ = _resolve_candidates(
        steady_candidates, aggressive_candidates
    )

    universe_filter = HistoricalUniverseFilter(
        status_table=inputs.status_table,
        quotes=inputs.quotes,
        **(universe_kwargs or _DEFAULT_UNIVERSE_KWARGS),
    )

    return ResearchRunner(
        bt_config=bt_config,
        benchmark=inputs.benchmark,
        universe_filter=universe_filter,
        walk_forward_config=wf_config,
        monte_carlo_config=mc_config,
        steady_candidates=steady_candidates,
        aggressive_candidates=aggressive_candidates,
    )


def execute_weekly_research(
    inputs: ResearchInputs,
    *,
    bt_config: Optional[BacktestConfig] = None,
    wf_config: Optional[WalkForwardConfig] = None,
    mc_config: Optional[MonteCarloConfig] = None,
    steady_candidates: Optional[dict[str, list]] = None,
    aggressive_candidates: Optional[dict[str, list]] = None,
    universe_kwargs: Optional[dict[str, Any]] = None,
    initial_cash: float = 1000.0,
) -> ResearchResult:
    """构建并运行真实 ``ResearchRunner``，返回完整 ``ResearchResult``。"""
    runner = build_research_runner(
        inputs,
        bt_config=bt_config,
        wf_config=wf_config,
        mc_config=mc_config,
        steady_candidates=steady_candidates,
        aggressive_candidates=aggressive_candidates,
        universe_kwargs=universe_kwargs,
    )
    trading_dates = sorted(
        d if isinstance(d, date) else pd.Timestamp(d).date()
        for d in inputs.quotes["trade_date"].unique()
    )
    return runner.run(inputs.quotes, trading_dates, initial_cash)


# ---------------------------------------------------------------------- #
# 摘要提取
# ---------------------------------------------------------------------- #


def _metrics_block(metrics: dict[str, Any]) -> dict[str, Optional[float]]:
    return {
        "total_return": metrics.get("total_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
    }


def _stress_block(stress_results: Sequence[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sr in stress_results or []:
        name = getattr(getattr(sr, "scenario", None), "name", None) or "unknown"
        out[name] = {
            "total_return": getattr(sr, "total_return", None),
            "max_drawdown": getattr(sr, "max_drawdown", None),
            "sharpe": getattr(sr, "sharpe", None),
        }
    return out


def _monte_carlo_block(mc: Any) -> dict[str, Any]:
    if mc is None:
        return {}
    return {
        "n_paths": getattr(mc, "n_paths", None),
        "prob_ten_x": getattr(mc, "prob_ten_x", None),
        "prob_loss_50": getattr(mc, "prob_loss_50", None),
        "prob_near_zero": getattr(mc, "prob_near_zero", None),
    }


def _regime_block(regime: Any) -> dict[str, Any]:
    if regime is None:
        return {}
    regimes = getattr(regime, "regimes", []) or []
    counts: dict[str, int] = {}
    for r in regimes:
        key = str(getattr(r, "regime", "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return {"day_count": len(regimes), "distribution": counts}


def _perturbation_block(pert: Any) -> dict[str, Any]:
    if pert is None:
        return {}
    return {
        "total_combinations": getattr(pert, "total_combinations", None),
        "positive_return_ratio": getattr(pert, "positive_return_ratio", None),
        "return_median": getattr(pert, "return_median", None),
        "baseline_return": getattr(pert, "baseline_return", None),
    }


def summarize_research(
    result: ResearchResult,
    *,
    data_hash: Optional[str] = None,
    config_hash: Optional[str] = None,
    code_commit: Optional[str] = None,
    candidate_counts: Optional[dict[str, int]] = None,
) -> ResearchSummary:
    """把完整 ``ResearchResult`` 精简为可序列化摘要。"""
    summary = ResearchSummary(
        ran=True,
        insufficient_sample=result.insufficient_sample,
        folds=len(result.folds),
        steady_eligibility=(
            result.steady.eligibility.status
            if result.steady.eligibility
            else None
        ),
        aggressive_eligibility=(
            result.aggressive.eligibility.status
            if result.aggressive.eligibility
            else None
        ),
        steady_total_return=result.steady.oos_metrics.get("total_return"),
        steady_max_drawdown=result.steady.oos_metrics.get("max_drawdown"),
        steady_sharpe=result.steady.oos_metrics.get("sharpe"),
        aggressive_total_return=result.aggressive.oos_metrics.get("total_return"),
        aggressive_max_drawdown=result.aggressive.oos_metrics.get("max_drawdown"),
        aggressive_sharpe=result.aggressive.oos_metrics.get("sharpe"),
        stress={
            "steady": _stress_block(result.steady.stress_results),
            "aggressive": _stress_block(result.aggressive.stress_results),
        },
        monte_carlo=_monte_carlo_block(result.aggressive.monte_carlo),
        regime=_regime_block(result.aggressive.market_regime),
        perturbation={
            "steady": _perturbation_block(result.steady.parameter_perturbation),
            "aggressive": _perturbation_block(result.aggressive.parameter_perturbation),
        },
        data_hash=data_hash,
        config_hash=config_hash,
        code_commit=code_commit,
        candidate_counts=candidate_counts or {},
        limitations=list(result.limitations),
    )
    return summary


# ---------------------------------------------------------------------- #
# 管线步骤
# ---------------------------------------------------------------------- #


def run_weekly_research_step(
    ctx: Any,
    *,
    research_dir: Path | str,
    loader: Optional[ResearchDataLoader] = None,
    runner_factory: Optional[Callable[..., ResearchResult]] = None,
    bt_config: Optional[BacktestConfig] = None,
    wf_config: Optional[WalkForwardConfig] = None,
    mc_config: Optional[MonteCarloConfig] = None,
    steady_candidates: Optional[dict[str, list]] = None,
    aggressive_candidates: Optional[dict[str, list]] = None,
    universe_kwargs: Optional[dict[str, Any]] = None,
    initial_cash: float = 1000.0,
    code_commit: Optional[str] = None,
) -> Optional[ResearchSummary]:
    """在每周管线内执行研究步骤，结果写入 ``ctx.scratch["weekly_research"]``。

    数据缺失 / 样本不足 / 运行异常都不会让周报失败——只记录原因并继续。
    """
    loader = loader or LocalResearchDataLoader(research_dir)

    with ctx.step("weekly_research") as step:
        try:
            inputs = loader.load()
        except ResearchDataUnavailable as exc:
            summary = ResearchSummary(ran=False, skipped_reason=str(exc))
            step.detail.update(summary.to_dict())
            ctx.scratch["weekly_research"] = summary.to_dict()
            return summary

        if inputs is None:
            summary = ResearchSummary(
                ran=False,
                skipped_reason=(
                    f"本地研究参考目录无完整三件套 "
                    f"(quotes/benchmark/security_status): {research_dir}"
                ),
            )
            step.detail.update(summary.to_dict())
            ctx.scratch["weekly_research"] = summary.to_dict()
            return summary

        try:
            steady, aggressive, n_steady, n_aggr = _resolve_candidates(
                steady_candidates, aggressive_candidates
            )
            candidate_counts = {"steady": n_steady, "aggressive": n_aggr}

            run = runner_factory or execute_weekly_research
            result = run(
                inputs,
                bt_config=bt_config,
                wf_config=wf_config,
                mc_config=mc_config,
                steady_candidates=steady,
                aggressive_candidates=aggressive,
                universe_kwargs=universe_kwargs,
                initial_cash=initial_cash,
            )

            data_hash = _hash_dataframe(inputs.quotes)
            config_dict = {
                "walk_forward": (wf_config or DEFAULT_WALK_FORWARD_CONFIG).__dict__,
                "monte_carlo": (mc_config or DEFAULT_MONTE_CARLO_CONFIG).__dict__,
                "candidates": {"steady": steady, "aggressive": aggressive},
            }
            config_hash = hashlib.sha256(
                json.dumps(config_dict, sort_keys=True, ensure_ascii=False).encode(
                    "utf-8"
                )
            ).hexdigest()

            summary = summarize_research(
                result,
                data_hash=data_hash,
                config_hash=config_hash,
                code_commit=code_commit,
                candidate_counts=candidate_counts,
            )
            step.detail.update(summary.to_dict())
            ctx.scratch["weekly_research"] = summary.to_dict()
            return summary
        except Exception as exc:  # noqa: BLE001 - 研究失败不应拖垮周报
            summary = ResearchSummary(
                ran=False,
                error=f"{type(exc).__name__}: {exc}",
                skipped_reason="研究运行异常，已跳过（不影响其余周报内容）",
            )
            step.detail.update(summary.to_dict())
            ctx.scratch["weekly_research"] = summary.to_dict()
            ctx.logger.warning(
                "weekly_research_failed",
                f"周度研究运行异常: {exc}",
                error=str(exc),
            )
            return summary
