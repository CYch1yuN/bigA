# 数据质量检查报告 (Phase 1)

- 严重问题 (critical): **0**
- 警告 (warning): **2**
- 总计: **2**
- 是否阻止下游: **否**
- 退出码: **0**
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
  "required_completeness": {
    "severity": "critical"
  },
  "raw_qfq_date_consistency": {
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
| abnormal_price_jump | warning | 000001 | 2024-01-23 | 异常价格跳变: 收益率绝对值超阈值 0.2 |
| abnormal_price_jump | warning | 000001 | 2024-01-24 | 异常价格跳变: 收益率绝对值超阈值 0.2 |