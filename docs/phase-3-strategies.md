# Phase 3 双轨策略与样本外研究文档

## 概述

Phase 3 在 Phase 2 回测器基础上实现双轨策略研究框架，包含稳健轨和激进轨两条独立策略轨道、滚动样本外验证、基准比较、压力测试和蒙特卡洛概率分析。本阶段仅实现策略研究和回测分析，不连接券商、不发送真实订单、不宣称策略可盈利。

## 架构

```
src/ashare_quant/research/
├── features.py          # 仅使用截至信号日数据的特征计算
├── universe.py          # 历史时点（point-in-time）股票池过滤
├── strategies.py        # 稳健轨、激进轨策略
├── benchmarks.py        # 沪深300、中证全指、现金基准
├── walk_forward.py      # 滚动训练/验证/测试切分
├── stress.py            # 费用、滑点、参数扰动、市场阶段
├── monte_carlo.py       # 激进轨概率分析
├── analysis.py          # 汇总指标和资格判定
└── report.py            # JSON、Markdown、Parquet 报告
```

配置文件：`config/strategy-research.default.yaml`，所有窗口、阈值、切分长度、随机种子、压力倍数和蒙特卡洛次数集中于此，禁止在代码中硬编码。

## 事件时序

### 信号生成与成交

所有策略严格遵守 Phase 2 的事件时序：

1. **收盘**：策略读取截至当日收盘的前复权数据，生成交易信号
2. **信号转订单**：收盘后信号转为订单，计划于下一交易日开盘成交
3. **开盘**：解冻 T+1 持仓，处理挂单（风控校验 → 撮合拒绝检查 → 执行成交）
4. **换仓顺序**：信号固定为先 SELL 后 BUY，不依赖负现金或同日卖出前尚未到账的资金

### 防未来函数保证

- 特征仅使用前复权 OHLCV 和截至信号日收盘的数据
- 成交、费用、估值和涨跌停使用未复权数据
- 滚动窗口使用 `min_periods=window` 确保历史不足时返回 NaN，不允许部分窗口计算
- 突破和量比的回看窗口使用 `shift(1)` 排除信号日
- 横截面 z-score 仅使用同一信号日通过股票池过滤的股票
- 所有数据切分先按日期完成，再计算训练期参数；禁止先在全样本拟合标准化器

## 历史时点股票池

每个信号日只能使用当日已经存在的信息。股票池过滤规则：

1. 排除当日为 ST、*ST、退市整理或已退市的股票
2. 排除截至当日上市交易不足 120 个交易日的股票
3. 排除过去 20 个交易日有效交易天数不足 15 天的股票
4. 排除过去 20 日平均成交额低于配置阈值（默认 20,000,000 元）的股票
5. 排除当日停牌、不可交易、无效价格或关键字段缺失的股票
6. 排除按下一交易日预计成交成本无法用当前可用现金购买一手的股票

上市、退市、名称和 ST 状态来自历史状态表并按日期做 point-in-time join。若数据源不能提供某项历史状态，报告标记数据缺陷并停止正式研究。

## 滚动样本外验证

### 固定研究协议

- **训练期**：连续 3 个交易年
- **训练内验证期**：训练期最后 6 个月，仅用于候选参数选择
- **样本外测试期**：紧随其后的 1 个交易年
- **步进**：1 个交易年
- 各测试期不得重叠；所有测试折必须完整展示
- 参数只在对应训练/验证数据上选择，选定后冻结并运行该折测试期
- 最终汇总只拼接各折样本外权益，不得用全样本重新挑选参数

### 样本要求

默认至少需要 5 个完整交易年。不足时允许运行"小样本演示"，但报告标记 `INSUFFICIENT_RESEARCH_SAMPLE`，不得给出实盘资格结论。

交易年优先按日历年边界切分；数据不足以构成完整日历年时，按 244 交易日近似一个交易年。

## 稳健轨策略

### 调仓规则

- 每周最后一个有效交易日收盘生成信号，下一交易日开盘成交
- 最多持有一只股票
- 无合格标的、现金不足或风险条件不满足时持有现金
- 换仓信号顺序固定为先 SELL 后 BUY

### 特征与得分

对通过股票池的标的计算：

| 特征 | 计算方式 | 基线参数 |
|------|---------|---------|
| 长期趋势 | `close_qfq / MA(trend_window) - 1`，且 `close_qfq > MA` | trend_window=120 |
| 动量 | `close_qfq / close_qfq.shift(momentum_window) - 1` | momentum_window=60 |
| 波动率 | 过去 20 日复权日收益标准差 × sqrt(244) | volatility_window=20 |

横截面得分：`zscore(trend) + zscore(momentum) - zscore(volatility)`

- 基线选择得分最高的一只
- 若最高得分小于 `minimum_score`（基线 0.0），保持现金
- z-score 必须在当日横截面计算；样本数小于 5 时不生成买入信号

### 参数候选集合

```yaml
steady:
  trend_window: [100, 120, 140]
  momentum_window: [50, 60, 70]
  volatility_window: [15, 20, 25]
  minimum_score: [-0.25, 0.0, 0.25]
```

### 训练内参数选择目标

1. 先排除最大回撤超过 20% 的候选
2. 按扣费后验证期 Calmar 比率排序
3. 并列时依次选择换手率更低、参数离基线更近的候选
4. 若没有候选满足条件，该折稳健轨全程持有现金并记录原因

## 激进轨策略

### 调仓规则

- 每个交易日收盘计算，下一交易日开盘成交
- 最多持有一只股票，仅用于模拟研究
- 换仓信号顺序固定为先 SELL 后 BUY

### 入场条件

基线入场必须同时满足：

1. `close_qfq > 前 20 个交易日最高收盘价`（窗口不含信号日）
2. 当日成交量 / 前 20 日平均成交量 `>= 1.5`（均值不含信号日）
3. 个股 20 日收益减沪深 300 同期收益 `> 0`
4. 多个候选按 `突破幅度 + 相对强度 + log(量比)` 的当日横截面 z-score 合计排序

### 退出条件

任一满足即退出：

1. 收盘价低于前 10 日最低收盘价（窗口不含信号日）
2. 持有满 20 个交易日
3. 标的不再满足历史股票池资格

### 参数候选集合

```yaml
aggressive:
  breakout_window: [15, 20, 25]
  volume_window: [15, 20, 25]
  volume_ratio: [1.2, 1.5, 1.8]
  relative_strength_window: [15, 20, 25]
  exit_low_window: [8, 10, 12]
  max_holding_days: [15, 20, 25]
```

### 训练内参数选择目标

验证期扣费后几何收益；并列时依次选择最大回撤更低、换手率更低、参数离基线更近的候选。不得以"一年十倍次数"作为参数选择目标。

## 基准比较

每个完整样本期和每个样本外折必须比较：

| 基准 | 说明 |
|------|------|
| 沪深 300 | 总收益或可获得的价格收益指数 |
| 中证全指 | 若免费数据源无法稳定取得，停止正式研究并报告缺失 |
| 现金基准 | 收益固定为 0%，保留实际未投资天数 |

基准统一按测试期首个可用收盘至期末收盘计算，不模拟股票交易费用。缺失基准日期按交易日交集对齐，不允许后向使用未来值。

## 压力测试

### 费用与滑点

每条轨道、每个样本外折至少运行：

| 场景 | 佣金/印花税/过户费 | 滑点 |
|------|-------------------|------|
| 基线 | 1x | 1x |
| 费用 2 倍 | 2x | 1x |
| 滑点 2 倍 | 1x | 2x |
| 联合压力 | 2x | 2x |

最低佣金仍按 Phase 2 规则执行，不能简单把最终收益乘折扣。

### 参数扰动

- 报告全部固定候选集合的样本外结果分布
- 报告基线参数相邻组合，不得只输出最佳组合
- 至少输出收益、最大回撤和换手率的中位数、10/90 分位数及正收益组合比例

### 市场阶段

使用沪深 300 的 120 日均线和 20 日实现波动率，按每个测试日当时可见数据划分：

| 阶段 | 条件 |
|------|------|
| 牛市 | 指数收盘高于 MA120，且 20 日年化波动率不高于训练期中位数 |
| 熊市 | 指数收盘低于 MA120 |
| 高波动 | 20 日年化波动率高于训练期 75 分位数 |

高波动可与牛/熊重叠。阶段阈值只能由对应训练期确定，不能使用全样本分位数。

## 蒙特卡洛分析

仅对拼接后的激进轨样本外日收益执行：

| 参数 | 默认值 |
|------|-------|
| 随机种子 | 20260731 |
| 路径数 | 10,000 |
| 每条路径 | 244 个交易日 |
| 方法 | 长度 5 日的 moving-block bootstrap，有放回抽取 |
| 初始资金 | 1,000 元（不额外放大仓位、不使用融资） |

### 报告指标

- 一年期末资金达到或超过 10,000 元的概率
- 一年内任意时点回撤或资金损失达到 50% 的概率
- 近似归零概率（期末资金不高于 100 元）
- 期末资金的 1%/5%/25%/50%/75%/95%/99% 分位数
- 使用的样本外天数、块长度、路径数和随机种子

若拼接样本外日收益少于 244 天，仍可输出演示结果，但标记 `INSUFFICIENT_MONTE_CARLO_SAMPLE`，不得作概率结论。

## 资格判定

### 稳健轨

只有同时满足以下全部条件才可标记 `ELIGIBLE_FOR_PAPER_OBSERVATION`：

1. 拼接样本外最大回撤不超过 20%
2. 扣除费用和滑点后的拼接样本外年化收益大于 0
3. 至少 60% 的样本外折收益为正
4. 费用与滑点联合 2 倍压力下年化收益仍大于 0
5. 基线相邻参数组合的样本外年化收益中位数大于 0
6. 无数据质量失败、未来函数、账务错误或不可解释权益变化

任一条件不满足，结论为 `NOT_ELIGIBLE_FOR_LIVE_TRADING`，并逐条列出失败原因。即使满足，也只允许进入模拟观察，不代表允许实盘。

### 激进轨

永远输出 `SIMULATION_ONLY`，不得输出实盘资格。

## CLI 使用

```bash
ashare-quant research \
  --quotes <curated-quotes.parquet> \
  --status <historical-status.parquet> \
  --benchmarks <benchmark.parquet> \
  --config <strategy-research.yaml> \
  --output <dir>
```

### 参数

| 参数 | 必需 | 说明 |
|------|------|------|
| `--quotes` | 是 | curated 日行情 Parquet 路径 |
| `--status` | 是 | 历史状态表 Parquet 路径 |
| `--benchmarks` | 是 | 基准数据 Parquet 路径 |
| `--config` | 否 | 策略研究配置 YAML（默认 `config/strategy-research.default.yaml`） |
| `--backtest-config` | 否 | 回测配置 YAML（默认 `config/backtest.default.yaml`） |
| `--output` | 否 | 报告输出目录（默认 `reports/phase-3`） |

### 输出文件

| 文件 | 说明 |
|------|------|
| `research-summary.json` | 完整 JSON 研究结果 |
| `research-report.md` | Markdown 可读报告 |
| `walk-forward-folds.parquet` | 各折切分日期和参数选择 |
| `steady-oos-equity.parquet` | 稳健轨样本外权益曲线 |
| `aggressive-oos-equity.parquet` | 激进轨样本外权益曲线 |
| `orders.parquet` | 订单流水 |
| `fills.parquet` | 成交流水 |
| `parameter-results.parquet` | 参数扰动结果 |
| `stress-results.parquet` | 压力测试结果 |
| `market-regimes.parquet` | 市场阶段分类 |
| `monte-carlo-summary.json` | 蒙特卡洛分析摘要 |

## 报告可追溯性

报告记录以下信息确保可追溯和可复现：

- 数据文件 SHA-256 哈希
- 配置文件 SHA-256 哈希
- 代码提交号（40 位 SHA）
- 全部限制声明
- 候选参数全集和选择过程
- 随机种子
- 内容哈希

相同数据、配置和代码提交必须产生字节级一致的 JSON/Markdown 和内容一致的 Parquet。

## 测试

### 测试覆盖

| 测试文件 | 覆盖范围 |
|---------|---------|
| `test_research_features.py` | 特征计算（MA、动量、波动率、突破、量比、相对强度、z-score） |
| `test_research_universe.py` | 历史时点股票池过滤（ST、退市、上市不足、停牌、流动性） |
| `test_research_strategies.py` | 稳健轨和激进轨策略逻辑 |
| `test_research_walk_forward.py` | 滚动切分、日期分离、防泄漏 |
| `test_research_benchmarks.py` | 基准比较、日期对齐、现金基准 |
| `test_research_stress.py` | 费用/滑点压力、参数扰动、市场阶段 |
| `test_research_monte_carlo.py` | 蒙特卡洛固定种子复现、概率定义 |
| `test_research_analysis.py` | 参数选择、资格判定、指标计算 |
| `test_research_report.py` | 报告生成、双跑一致性 |
| `test_research_integration.py` | ResearchRunner 端到端集成 |

### 运行测试

```bash
# 全量测试
python -m pytest tests/ -v

# 仅研究模块测试
python -m pytest tests/test_research_*.py -v

# 覆盖率报告
python -m pytest tests/ --cov=ashare_quant --cov-report=term-missing
```

### 合成测试数据

合成测试数据覆盖以下场景：

- 牛市（稳定上涨）、熊市（稳定下跌）、高波动
- 停牌、涨跌停
- ST、退市、上市不足 120 日
- 无法购买一手（价格过高）
- 基准缺失日期

## 限制声明

- Phase 3 不处理分红、送股、拆并股和配股
- 前复权价格仅用于信号和特征计算，未复权价格用于成交
- 仅支持下一交易日开盘市价撮合
- 不支持限价单、部分成交和盘中撮合
- 蒙特卡洛分析仅用于概率研究，不构成收益承诺
- 激进轨永远为 `SIMULATION_ONLY`，不得用于实盘
- "一年十倍"只作为激进轨概率研究指标，不是目标函数、承诺或 Gate 3 通过条件

## 复现实验

### 金标准报告

```bash
# 1. 生成合成测试数据
python -c "
import sys; sys.path.insert(0, 'src')
from tests.research_samples import make_research_quotes, make_historical_status_table, make_benchmark_data, make_trade_dates_range
from datetime import date
from pathlib import Path
out = Path('reports/phase-3/golden-input'); out.mkdir(parents=True, exist_ok=True)
dates = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
quotes = make_research_quotes(start=date(2019, 1, 2), n_days=244*5, n_stocks=8)
quotes.to_parquet(out / 'golden-quotes.parquet', index=False)
status = make_historical_status_table(start=date(2019, 1, 2), n_stocks=8)
status.to_parquet(out / 'golden-status.parquet', index=False)
benchmark = make_benchmark_data(start=date(2019, 1, 2), n_days=len(dates))
benchmark.to_parquet(out / 'golden-benchmarks.parquet', index=False)
"

# 2. 运行研究 CLI
ashare-quant research \
  --quotes reports/phase-3/golden-input/golden-quotes.parquet \
  --status reports/phase-3/golden-input/golden-status.parquet \
  --benchmarks reports/phase-3/golden-input/golden-benchmarks.parquet \
  --config reports/phase-3/golden-input/strategy-research.golden.yaml \
  --output reports/phase-3

# 3. 验证双跑一致性
python -m pytest tests/test_research_report.py::TestDoubleRunConsistency -v
```

### 正式研究

使用真实数据和默认配置运行正式研究：

```bash
ashare-quant research \
  --quotes data/curated-quotes.parquet \
  --status data/historical-status.parquet \
  --benchmarks data/benchmark.parquet \
  --config config/strategy-research.default.yaml \
  --output reports/phase-3
```

## 交付状态

- 全部测试通过（1029+ tests）
- 总覆盖率 ≥ 90%
- `ashare_quant.research` 包覆盖率 ≥ 90%
- 金标准示例报告已生成至 `reports/phase-3/`
- 等待 Codex Gate 3 审核
