# 数据质量检查报告 (Phase 1)

- 严重问题 (critical): **2**
- 警告 (warning): **0**
- 总计: **2**
- 是否阻止下游: **是**
- 退出码: **1**
- schema 版本: `1.0.0`

## 配置摘要

```json
{
  "duplicate_primary_key": {
    "severity": "critical"
  },
  "missing_trade_day": {
    "severity": "critical"
  },
  "ohlc_relation": {
    "severity": "critical"
  },
  "negative_price": {
    "severity": "critical"
  },
  "negative_volume": {
    "severity": "critical"
  },
  "abnormal_volume": {
    "severity": "warning",
    "lookback_days": 20,
    "median_ratio_threshold": 20.0
  },
  "abnormal_price_jump": {
    "severity": "warning",
    "abs_return_threshold": 0.2
  },
  "adjustment_factor_continuity": {
    "severity": "critical",
    "max_factor_ratio": 5.0
  },
  "status_contradiction": {
    "severity": "critical"
  },
  "cross_source": {
    "severity": "warning",
    "close_relative_tolerance": 0.02,
    "volume_relative_tolerance": 0.1
  }
}
```

## 问题明细

| 检查 | 严重等级 | 代码 | 交易日 | 描述 |
| --- | --- | --- | --- | --- |
| duplicate_primary_key | critical | 000001 | 2024-01-09 | 主键 (symbol, trade_date) 重复 |
| duplicate_primary_key | critical | 000001 | 2024-01-09 | 主键 (symbol, trade_date) 重复 |