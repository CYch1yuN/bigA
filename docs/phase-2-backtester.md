# Phase 2 回测器文档

## 概述

Phase 2 实现了事件驱动的 A 股回测器，支持 T+1 交易规则、100 股整数倍手数、涨跌停限制、可配置费用结构、滑点模拟和完整审计报告。本阶段仅实现回测基础设施，不包含策略层。

## 架构

```
src/ashare_quant/backtest/
├── __init__.py        # 包导出
├── models.py          # 数据模型（Signal, Order, Fill, Position, PortfolioSnapshot, BacktestResult）
├── config.py          # Pydantic 配置模型与 YAML 加载
├── interfaces.py      # 抽象接口（Strategy, UniverseFilter, RiskManager, BrokerSimulator, BacktestEngine）
├── engine.py          # 事件驱动回测引擎
├── broker.py          # A 股成交模拟器（滑点、费用、涨跌停）
├── risk.py            # 默认风控管理器
├── universe.py        # 默认股票池过滤器
├── metrics.py         # 指标计算器
├── report.py          # JSON/Markdown/Parquet 报告生成
└── strategies.py      # 测试用确定性策略（ScriptedStrategy, NoOpStrategy）
```

## 事件时序

每个交易日的处理顺序严格固定：

1. **开盘**：解冻 T+1 持仓（前日买入转为可卖）
2. **开盘**：处理挂单（风控校验 → 撮合拒绝检查 → 执行成交 → 更新现金/持仓）
3. **收盘**：估值（按未复权收盘价计算持仓市值）
4. **收盘**：策略读取截至当日收盘数据，生成下一交易日信号
5. **收盘**：信号转为订单，计划于下一交易日开盘成交

防未来函数保证：策略只能读取截至信号日收盘的前复权数据，信号最早在下一交易日开盘以未复权价格成交。

## A 股成交规则

### 费用结构

| 费用类型 | 方向 | 默认费率 | 说明 |
|---------|------|---------|------|
| 佣金 | 买卖双向 | 0.03% (万三) | 最低 5 元 |
| 印花税 | 仅卖出 | 0.1% (千一) | 买入不收 |
| 过户费 | 买卖双向 | 0.001% (万零点一) | 双向收取 |

### 滑点

- 买入：开盘价 + bps 滑点，向上取整到 0.01 元 tick
- 卖出：开盘价 - bps 滑点，向下取整到 0.01 元 tick
- 默认 10 bps (0.1%)

### 涨跌停

| 板块 | 涨跌停比例 |
|------|-----------|
| 沪市主板 | 10% |
| 深市主板/创业板 | 10% |
| 科创板 | 20% |
| 北交所 | 30% |
| ST 股票 | 5% |

保守撮合：开盘达到涨停拒绝买入，达到跌停拒绝卖出。涨停允许卖出，跌停允许买入。

### T+1 规则

- 买入当日持仓冻结（`frozen_buy_quantity`），不可卖出
- 下一交易日开盘解冻，转为可卖数量（`sellable_quantity`）
- 卖出不得超过可卖数量

### 手数

- 买入数量必须为 100 股整数倍，不合法直接拒绝，不自动调整
- 卖出数量必须为 100 股整数倍

## 配置

所有参数集中在 `config/backtest.default.yaml`，代码不硬编码费率：

```yaml
initial_cash: 1000.0
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
```

## CLI 使用

```bash
ashare-quant backtest \
  --quotes <curated.parquet> \
  --config <yaml> \
  --strategy scripted \
  --signals <json> \
  --output <dir>
```

### 参数

| 参数 | 必需 | 说明 |
|------|------|------|
| `--quotes` | 是 | curated 日行情 Parquet 路径 |
| `--config` | 否 | 回测配置 YAML 路径（默认使用 `config/backtest.default.yaml`） |
| `--strategy` | 否 | 策略类型：`scripted`（默认）或 `noop` |
| `--signals` | scripted 时必需 | 信号 JSON 文件路径 |
| `--start` | 否 | 回测起始日 YYYY-MM-DD |
| `--end` | 否 | 回测结束日 YYYY-MM-DD |
| `--output` | 否 | 报告输出目录（默认 `reports/phase-2`） |

### 信号 JSON 格式

```json
[
  {
    "signal_date": "2024-01-02",
    "symbol": "000001",
    "side": "BUY",
    "quantity": 100,
    "reason": "买入信号"
  }
]
```

### 输出文件

| 文件 | 说明 |
|------|------|
| `backtest-result.json` | 完整 JSON 结果（含指标、订单、成交、权益） |
| `backtest-report.md` | Markdown 可读报告 |
| `orders.parquet` | 订单流水 |
| `fills.parquet` | 成交流水 |
| `equity.parquet` | 每日权益 |

## 指标

报告包含以下指标：

- 初始/最终权益
- 总收益率、年化收益率
- 最大回撤
- 交易次数、胜率、盈亏比
- 换手率
- 不可成交订单率
- 拒绝原因计数
- 每日现金/市值/权益

无法定义的指标使用 `null`（如零交易时的胜率）。

## 限制声明

- Phase 2 不处理分红、送股、拆并股和配股
- 前复权价格仅用于信号，未复权价格用于成交
- 仅支持下一交易日开盘市价撮合
- 不支持限价单、部分成交和盘中撮合

## 测试

### 测试覆盖

| 测试文件 | 覆盖范围 |
|---------|---------|
| `test_backtest_models.py` | 数据模型验证 |
| `test_backtest_broker.py` | 成交模拟器（滑点、费用、涨跌停） |
| `test_backtest_risk.py` | 风控管理器 |
| `test_backtest_universe.py` | 股票池过滤 |
| `test_backtest_engine.py` | 引擎集成（防未来函数、T+1、账务恒等式） |
| `test_backtest_golden.py` | 人工金标准核对 |
| `test_backtest_cli.py` | CLI 端到端 |

### 运行测试

```bash
python -m pytest tests/ -v
python -m pytest tests/ --cov=ashare_quant --cov-report=term-missing
```

## 交付状态

- 全部 352 个测试通过
- 总覆盖率 91%
- 回测核心包覆盖率 ≥ 90%
- 金标准示例报告已生成至 `reports/phase-2/`
