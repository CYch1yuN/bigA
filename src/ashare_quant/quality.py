"""数据质量检查模块。

实现 10 类检查，严重等级与阈值全部来自 YAML 配置：
1. 主键重复  2. 缺失交易日  3. OHLC 关系  4. 负价格  5. 负成交量/成交额
6. 异常成交量  7. 异常价格跳变  8. 复权连续性  9. 股票状态矛盾  10. 跨源差异

严重（critical）问题阻止下游并返回非零退出码；同时生成 JSON 与 Markdown 报告。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

import numpy as np
import pandas as pd

from .config import AppConfig, SEVERITY_CRITICAL, SEVERITY_WARNING
from .constants import (
    DAILY_QUOTE_PRIMARY_KEY,
    SECURITY_MASTER_PRIMARY_KEY,
    SOURCE_AKSHARE,
    SOURCE_BAOSTOCK,
)


@dataclass
class Issue:
    """单条质量问题。"""

    check: str
    severity: str
    symbol: str
    trade_date: Optional[str]
    description: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    """质量检查报告。"""

    issues: list[Issue] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    config_summary: dict[str, Any] = field(default_factory=dict)
    schema_version: str = ""

    @property
    def has_critical(self) -> bool:
        return any(i.severity == SEVERITY_CRITICAL for i in self.issues)

    @property
    def exit_code(self) -> int:
        return 1 if self.has_critical else 0

    def counts(self) -> dict[str, int]:
        crit = sum(1 for i in self.issues if i.severity == SEVERITY_CRITICAL)
        warn = sum(1 for i in self.issues if i.severity == SEVERITY_WARNING)
        return {"critical": crit, "warning": warn, "total": len(self.issues)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {**self.summary, **self.counts()},
            "config_summary": self.config_summary,
            "schema_version": self.schema_version,
            "has_critical": self.has_critical,
            "exit_code": self.exit_code,
            "issues": [asdict(i) for i in self.issues],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# 数据质量检查报告 (Phase 1)")
        lines.append("")
        c = self.counts()
        lines.append(f"- 严重问题 (critical): **{c['critical']}**")
        lines.append(f"- 警告 (warning): **{c['warning']}**")
        lines.append(f"- 总计: **{c['total']}**")
        lines.append(f"- 是否阻止下游: **{'是' if self.has_critical else '否'}**")
        lines.append(f"- 退出码: **{self.exit_code}**")
        lines.append(f"- schema 版本: `{self.schema_version}`")
        lines.append("")
        if self.config_summary:
            lines.append("## 配置摘要")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(self.config_summary, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
        lines.append("## 问题明细")
        lines.append("")
        if not self.issues:
            lines.append("无质量问题。")
            return "\n".join(lines)
        lines.append("| 检查 | 严重等级 | 代码 | 交易日 | 描述 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for i in self.issues:
            td = i.trade_date or ""
            desc = i.description.replace("|", "/")
            lines.append(
                f"| {i.check} | {i.severity} | {i.symbol} | {td} | {desc} |"
            )
        return "\n".join(lines)


class QualityChecker:
    """质量检查器，阈值来自配置。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.schema_version = config.schema_versions.daily_quote_version

    def run(
        self,
        df: pd.DataFrame,
        security_master: Optional[pd.DataFrame] = None,
        trade_calendar: Optional[pd.DataFrame] = None,
        cross_source_df: Optional[pd.DataFrame] = None,
        request_start: Optional[str] = None,
        request_end: Optional[str] = None,
    ) -> QualityReport:
        """运行全部检查。

        参数:
            df: curated 日行情。
            security_master: curated 证券主数据（可选，用于状态矛盾检查与上市/退市边界）。
            trade_calendar: 交易日历 DataFrame，含 ``trade_date`` 列（可选）。
            cross_source_df: 另一数据源的 curated 日行情（可选，用于跨源比较）。
            request_start: 请求起始日期 YYYY-MM-DD（可选，用于首尾截断检测）。
            request_end: 请求结束日期 YYYY-MM-DD（可选，用于首尾截断检测）。
        """
        report = QualityReport(schema_version=self.schema_version)
        report.config_summary = self._config_summary()
        report.summary = {
            "rows_checked": int(len(df)),
            "symbols_checked": int(df["symbol"].nunique()) if not df.empty else 0,
        }

        if df.empty:
            report.issues.append(
                Issue(
                    check="empty_data",
                    severity=SEVERITY_CRITICAL,
                    symbol="*",
                    trade_date=None,
                    description="curated 日行情为空",
                )
            )
            return report

        report.issues += self._check_duplicate_pk(df)
        report.issues += self._check_required_completeness(df)
        report.issues += self._check_missing_trade_days(
            df, trade_calendar, request_start, request_end, security_master
        )
        report.issues += self._check_raw_qfq_date_consistency(df)
        report.issues += self._check_ohlc(df)
        report.issues += self._check_negative_price(df)
        report.issues += self._check_negative_volume(df)
        report.issues += self._check_abnormal_volume(df)
        report.issues += self._check_price_jump(df)
        report.issues += self._check_adjustment_continuity(df)
        report.issues += self._check_status_contradiction(df, security_master)
        if cross_source_df is not None and not cross_source_df.empty:
            report.issues += self._check_cross_source(df, cross_source_df)

        return report

    # ---- 各检查实现 ----
    def _check_duplicate_pk(self, df: pd.DataFrame) -> list[Issue]:
        rule = self.config.quality_rule("duplicate_primary_key")
        dup = df.duplicated(subset=DAILY_QUOTE_PRIMARY_KEY, keep=False)
        issues: list[Issue] = []
        if dup.any():
            for _, row in df[dup].head(50).iterrows():
                issues.append(
                    Issue(
                        check="duplicate_primary_key",
                        severity=rule.severity,
                        symbol=str(row["symbol"]),
                        trade_date=str(row["trade_date"]),
                        description="主键 (symbol, trade_date) 重复",
                        details={},
                    )
                )
        return issues

    def _check_missing_trade_days(
        self,
        df: pd.DataFrame,
        cal: Optional[pd.DataFrame],
        request_start: Optional[str] = None,
        request_end: Optional[str] = None,
        security_master: Optional[pd.DataFrame] = None,
    ) -> list[Issue]:
        """检查缺失交易日。

        G1-05 修复：
        - 显式接收请求起止日期，结合交易日历计算预期日期。
        - 结合证券主数据的上市/退市边界排除上市前和退市后的日期。
        - 未提供请求范围时回退到数据实际范围（向后兼容）。
        """
        rule = self.config.quality_rule("missing_trade_day")
        issues: list[Issue] = []
        if cal is None or cal.empty:
            return issues
        cal_dates = set(pd.to_datetime(cal["trade_date"]).dt.date)

        # 构建 symbol -> (最早上市日, 最晚退市日) 映射
        sm_map: dict[str, tuple[Optional[date], Optional[date]]] = {}
        if security_master is not None and not security_master.empty:
            for _, r in security_master.iterrows():
                sym = str(r["symbol"]).strip().zfill(6)
                ld = (
                    pd.to_datetime(r["list_date"]).date()
                    if pd.notna(r.get("list_date"))
                    else None
                )
                dd = (
                    pd.to_datetime(r["delist_date"]).date()
                    if pd.notna(r.get("delist_date"))
                    else None
                )
                prev_ld, prev_dd = sm_map.get(sym, (None, None))
                if ld is not None and (prev_ld is None or ld < prev_ld):
                    prev_ld = ld
                if dd is not None and (prev_dd is None or dd > prev_dd):
                    prev_dd = dd
                sm_map[sym] = (prev_ld, prev_dd)

        for symbol, grp in df.groupby("symbol"):
            grp = grp.sort_values("trade_date")
            present = set(grp["trade_date"])

            # 确定预期区间
            if request_start is not None and request_end is not None:
                range_min = pd.to_datetime(request_start).date()
                range_max = pd.to_datetime(request_end).date()
            else:
                range_min = grp["trade_date"].min()
                range_max = grp["trade_date"].max()

            # 结合上市/退市边界收窄区间
            sym_key = str(symbol).strip().zfill(6)
            list_date, delist_date = sm_map.get(sym_key, (None, None))
            if list_date is not None and list_date > range_min:
                range_min = list_date
            if delist_date is not None and delist_date < range_max:
                range_max = delist_date

            expected = {d for d in cal_dates if range_min <= d <= range_max}
            missing = sorted(expected - present)
            for d in missing:
                issues.append(
                    Issue(
                        check="missing_trade_day",
                        severity=rule.severity,
                        symbol=str(symbol),
                        trade_date=str(d),
                        description="在请求区间内缺失交易日",
                        details={
                            "range_min": str(range_min),
                            "range_max": str(range_max),
                        },
                    )
                )
        return issues

    def _check_required_completeness(self, df: pd.DataFrame) -> list[Issue]:
        """检查必需字段完整性：raw/qfq OHLC、成交量、成交额、复权因子缺失或非有限值。

        G1-01 修复：NaN/Inf 在必需数值字段中必须触发 critical。
        """
        rule = self.config.quality_rule("required_completeness")
        issues: list[Issue] = []
        required = [
            "open_raw", "high_raw", "low_raw", "close_raw",
            "open_qfq", "high_qfq", "low_qfq", "close_qfq",
            "volume", "amount", "adjustment_factor",
        ]
        for col in required:
            if col not in df.columns:
                issues.append(
                    Issue(
                        check="required_completeness",
                        severity=rule.severity,
                        symbol="*",
                        trade_date=None,
                        description=f"必需字段缺失: {col}",
                        details={},
                    )
                )
                continue
            col_vals = pd.to_numeric(df[col], errors="coerce").astype(float)
            mask = col_vals.isna() | ~np.isfinite(col_vals)
            if mask.any():
                bad_rows = df[mask].head(50)
                for _, row in bad_rows.iterrows():
                    val = row[col]
                    detail_val = (
                        float(val) if pd.notna(val) and np.isfinite(float(val)) else None
                    )
                    issues.append(
                        Issue(
                            check="required_completeness",
                            severity=rule.severity,
                            symbol=str(row["symbol"]),
                            trade_date=str(row["trade_date"]),
                            description=f"字段 {col} 包含 NaN/非有限值",
                            details={col: detail_val},
                        )
                    )
        return issues

    def _check_raw_qfq_date_consistency(self, df: pd.DataFrame) -> list[Issue]:
        """检查 raw 与 qfq 日期集合是否一致。

        G1-01 修复：任一交易日 raw 有效但 qfq 缺失必须 critical。
        """
        rule = self.config.quality_rule("raw_qfq_date_consistency")
        issues: list[Issue] = []
        raw_cols = ["open_raw", "high_raw", "low_raw", "close_raw"]
        qfq_cols = ["open_qfq", "high_qfq", "low_qfq", "close_qfq"]

        for symbol, grp in df.groupby("symbol"):
            raw_df = grp[raw_cols].apply(pd.to_numeric, errors="coerce")
            qfq_df = grp[qfq_cols].apply(pd.to_numeric, errors="coerce")
            # raw 有效：非 NaN 且非 inf
            raw_valid = raw_df.notna().all(axis=1) & (
                raw_df.abs() != float("inf")
            ).all(axis=1)
            qfq_valid = qfq_df.notna().all(axis=1) & (
                qfq_df.abs() != float("inf")
            ).all(axis=1)

            raw_dates = set(grp.loc[raw_valid, "trade_date"])
            qfq_dates = set(grp.loc[qfq_valid, "trade_date"])

            only_raw = raw_dates - qfq_dates
            only_qfq = qfq_dates - raw_dates

            for d in sorted(only_raw):
                issues.append(
                    Issue(
                        check="raw_qfq_date_consistency",
                        severity=rule.severity,
                        symbol=str(symbol),
                        trade_date=str(d),
                        description="存在 raw 数据但无 qfq 数据",
                        details={},
                    )
                )
            for d in sorted(only_qfq):
                issues.append(
                    Issue(
                        check="raw_qfq_date_consistency",
                        severity=rule.severity,
                        symbol=str(symbol),
                        trade_date=str(d),
                        description="存在 qfq 数据但无 raw 数据",
                        details={},
                    )
                )
        return issues

    def _check_ohlc(self, df: pd.DataFrame) -> list[Issue]:
        rule = self.config.quality_rule("ohlc_relation")
        issues: list[Issue] = []
        # 使用未复权列校验 OHLC 关系
        hi = df["high_raw"]
        lo = df["low_raw"]
        op = df["open_raw"]
        cl = df["close_raw"]
        bad = (hi < op) | (hi < cl) | (hi < lo) | (lo > op) | (lo > cl) | (lo > hi)
        for _, row in df[bad].head(50).iterrows():
            issues.append(
                Issue(
                    check="ohlc_relation",
                    severity=rule.severity,
                    symbol=str(row["symbol"]),
                    trade_date=str(row["trade_date"]),
                    description="OHLC 关系错误 (high/low 与 open/close 矛盾)",
                    details={
                        "open_raw": float(row["open_raw"]),
                        "high_raw": float(row["high_raw"]),
                        "low_raw": float(row["low_raw"]),
                        "close_raw": float(row["close_raw"]),
                    },
                )
            )
        return issues

    def _check_negative_price(self, df: pd.DataFrame) -> list[Issue]:
        rule = self.config.quality_rule("negative_price")
        issues: list[Issue] = []
        cols = ["open_raw", "high_raw", "low_raw", "close_raw"]
        bad = (df[cols] < 0).any(axis=1)
        for _, row in df[bad].head(50).iterrows():
            issues.append(
                Issue(
                    check="negative_price",
                    severity=rule.severity,
                    symbol=str(row["symbol"]),
                    trade_date=str(row["trade_date"]),
                    description="存在负价格",
                    details={c: float(row[c]) for c in cols},
                )
            )
        return issues

    def _check_negative_volume(self, df: pd.DataFrame) -> list[Issue]:
        rule = self.config.quality_rule("negative_volume")
        issues: list[Issue] = []
        bad = (df["volume"] < 0) | (df["amount"] < 0)
        for _, row in df[bad].head(50).iterrows():
            issues.append(
                Issue(
                    check="negative_volume",
                    severity=rule.severity,
                    symbol=str(row["symbol"]),
                    trade_date=str(row["trade_date"]),
                    description="负成交量或负成交额",
                    details={
                        "volume": float(row["volume"]),
                        "amount": float(row["amount"]),
                    },
                )
            )
        return issues

    def _check_abnormal_volume(self, df: pd.DataFrame) -> list[Issue]:
        rule = self.config.quality_rule("abnormal_volume")
        lookback = int(rule.params.get("lookback_days", 20))
        thresh = float(rule.params.get("median_ratio_threshold", 20.0))
        issues: list[Issue] = []
        for symbol, grp in df.sort_values("trade_date").groupby("symbol"):
            vol = grp["volume"].astype(float)
            med = vol.rolling(window=lookback, min_periods=max(3, lookback // 2)).median()
            ratio = vol / med.replace(0, pd.NA)
            bad = ratio > thresh
            for idx in grp.index[bad]:
                row = grp.loc[idx]
                issues.append(
                    Issue(
                        check="abnormal_volume",
                        severity=rule.severity,
                        symbol=str(symbol),
                        trade_date=str(row["trade_date"]),
                        description=f"异常成交量: 相对中位数倍数超阈值 {thresh}",
                        details={
                            "volume": float(row["volume"]),
                            "median": float(med.loc[idx]) if pd.notna(med.loc[idx]) else None,
                            "ratio": float(ratio.loc[idx]) if pd.notna(ratio.loc[idx]) else None,
                        },
                    )
                )
        return issues

    def _check_price_jump(self, df: pd.DataFrame) -> list[Issue]:
        rule = self.config.quality_rule("abnormal_price_jump")
        thresh = float(rule.params.get("abs_return_threshold", 0.20))
        issues: list[Issue] = []
        for symbol, grp in df.sort_values("trade_date").groupby("symbol"):
            ret = grp["close_qfq"].astype(float).pct_change(fill_method=None)
            bad = ret.abs() > thresh
            for idx in grp.index[bad]:
                row = grp.loc[idx]
                issues.append(
                    Issue(
                        check="abnormal_price_jump",
                        severity=rule.severity,
                        symbol=str(symbol),
                        trade_date=str(row["trade_date"]),
                        description=f"异常价格跳变: 收益率绝对值超阈值 {thresh}",
                        details={
                            "close_qfq": float(row["close_qfq"]),
                            "abs_return": float(ret.loc[idx]),
                        },
                    )
                )
        return issues

    def _check_adjustment_continuity(self, df: pd.DataFrame) -> list[Issue]:
        rule = self.config.quality_rule("adjustment_factor_continuity")
        max_ratio = float(rule.params.get("max_factor_ratio", 5.0))
        issues: list[Issue] = []
        for symbol, grp in df.sort_values("trade_date").groupby("symbol"):
            fac = grp["adjustment_factor"].astype(float)
            prev = fac.shift(1)
            # 相邻因子比值（较大/较小），跳过分母为 0
            pair = pd.concat([prev, fac], axis=1)
            pair.columns = ["prev", "cur"]
            valid = pair.dropna()
            valid = valid[(valid["prev"] != 0) & (valid["cur"] != 0)]
            if valid.empty:
                continue
            bigger = valid[["prev", "cur"]].max(axis=1)
            smaller = valid[["prev", "cur"]].min(axis=1)
            ratio = bigger / smaller
            bad_idx = ratio[ratio > max_ratio].index
            for idx in bad_idx:
                row = grp.loc[idx]
                issues.append(
                    Issue(
                        check="adjustment_factor_continuity",
                        severity=rule.severity,
                        symbol=str(symbol),
                        trade_date=str(row["trade_date"]),
                        description=f"复权因子跳变超阈值 (比值>{max_ratio})",
                        details={
                            "prev_factor": float(pair.loc[idx, "prev"]),
                            "cur_factor": float(fac.loc[idx]),
                        },
                    )
                )
        return issues

    def _check_status_contradiction(
        self, df: pd.DataFrame, sm: Optional[pd.DataFrame]
    ) -> list[Issue]:
        rule = self.config.quality_rule("status_contradiction")
        issues: list[Issue] = []
        # 1. 退市日早于上市日（需证券主数据）
        if sm is not None and not sm.empty and "list_date" in sm.columns and "delist_date" in sm.columns:
            for _, r in sm.iterrows():
                if pd.notna(r["list_date"]) and pd.notna(r["delist_date"]):
                    if r["delist_date"] < r["list_date"]:
                        issues.append(
                            Issue(
                                check="status_contradiction",
                                severity=rule.severity,
                                symbol=str(r["symbol"]),
                                trade_date=None,
                                description="退市日早于上市日",
                                details={
                                    "list_date": str(r["list_date"]),
                                    "delist_date": str(r["delist_date"]),
                                },
                            )
                        )
        # 2. 停牌日成交量 > 0（无需主数据）
        suspended_vol = df[(df["is_suspended"]) & (df["volume"] > 0)]
        for _, row in suspended_vol.head(50).iterrows():
            issues.append(
                Issue(
                    check="status_contradiction",
                    severity=rule.severity,
                    symbol=str(row["symbol"]),
                    trade_date=str(row["trade_date"]),
                    description="停牌标记为 True 但成交量大于 0",
                    details={"volume": float(row["volume"])},
                )
            )
        return issues

    def _check_cross_source(
        self, df: pd.DataFrame, other: pd.DataFrame
    ) -> list[Issue]:
        rule = self.config.quality_rule("cross_source")
        close_tol = float(rule.params.get("close_relative_tolerance", 0.02))
        vol_tol = float(rule.params.get("volume_relative_tolerance", 0.10))
        issues: list[Issue] = []
        merged = df.merge(
            other[["symbol", "trade_date", "close_raw", "volume"]],
            on=["symbol", "trade_date"],
            suffixes=("", "_other"),
            how="inner",
        )
        for _, row in merged.iterrows():
            c1 = float(row["close_raw"])
            c2 = float(row["close_raw_other"])
            v1 = float(row["volume"])
            v2 = float(row["volume_other"])
            if c1 != 0 and abs(c1 - c2) / abs(c1) > close_tol:
                issues.append(
                    Issue(
                        check="cross_source",
                        severity=rule.severity,
                        symbol=str(row["symbol"]),
                        trade_date=str(row["trade_date"]),
                        description=f"跨源收盘价差异超容忍度 {close_tol}",
                        details={"close_self": c1, "close_other": c2},
                    )
                )
            if v1 != 0 and abs(v1 - v2) / abs(v1) > vol_tol:
                issues.append(
                    Issue(
                        check="cross_source",
                        severity=rule.severity,
                        symbol=str(row["symbol"]),
                        trade_date=str(row["trade_date"]),
                        description=f"跨源成交量差异超容忍度 {vol_tol}",
                        details={"volume_self": v1, "volume_other": v2},
                    )
                )
        return issues

    def _config_summary(self) -> dict[str, Any]:
        """提取可追溯的配置摘要。"""
        summary: dict[str, Any] = {}
        for name in self.config.quality:
            try:
                rule = self.config.quality_rule(name)
                summary[name] = {"severity": rule.severity, **rule.params}
            except (KeyError, TypeError):
                summary[name] = str(self.config.quality[name])
        return summary


__all__ = ["Issue", "QualityReport", "QualityChecker"]
