# 测试覆盖报告 (Phase 1)

生成时间: 2026-07-30T10:09:34.552299+00:00

## 覆盖率摘要

- 语句覆盖率: **88.08%**
- 已覆盖语句: 1101
- 未覆盖语句: 149
- 总语句数: 1250

## 各文件覆盖率

| 文件 | 覆盖率 | 已覆盖 | 总数 |
| --- | --- | --- | --- |
| src\ashare_quant\__init__.py | 100.0% | 2 | 2 |
| src\ashare_quant\cli.py | 99.0% | 192 | 194 |
| src\ashare_quant\config.py | 93.3% | 56 | 60 |
| src\ashare_quant\constants.py | 100.0% | 16 | 16 |
| src\ashare_quant\fetcher.py | 81.6% | 80 | 98 |
| src\ashare_quant\manifest.py | 94.3% | 33 | 35 |
| src\ashare_quant\providers\__init__.py | 100.0% | 4 | 4 |
| src\ashare_quant\providers\akshare_provider.py | 78.7% | 59 | 75 |
| src\ashare_quant\providers\baostock_provider.py | 50.5% | 53 | 105 |
| src\ashare_quant\providers\base.py | 93.8% | 15 | 16 |
| src\ashare_quant\quality.py | 96.1% | 273 | 284 |
| src\ashare_quant\reports.py | 78.3% | 36 | 46 |
| src\ashare_quant\samples.py | 97.3% | 72 | 74 |
| src\ashare_quant\schema.py | 85.7% | 54 | 63 |
| src\ashare_quant\standardize.py | 93.1% | 95 | 102 |
| src\ashare_quant\storage.py | 80.3% | 61 | 76 |
## 离线测试说明

所有单元测试离线运行，不调用 AKShare/BaoStock 公网接口。
数据提供器通过 mock/fixture 测试；合成样本覆盖正常、重复、缺失、停牌、退市、
ST 区间、OHLC 错误、负成交量、异常跳变与双源冲突等场景。
