# Trae 任务：Phase 2 回测器

## 角色与边界

你是本阶段实现者，使用 GLM-5.2 完成回测基础设施、测试和文档。工作分支固定为 `trae/phase-2-backtester`，基于最新 `main` 创建。

本阶段只实现事件驱动回测器、股票池过滤接口、风控接口和可审计报告；不得实现稳健轨、激进轨、参数优化、蒙特卡洛、自动任务或券商连接。测试策略只能是确定性的脚本策略，用来验证回测器。

开始前完整阅读 `docs/review-gates.md`、本任务书、`docs/reviews/gate-1-final-review.md`，以及 Phase 1现有schema、quality、storage和测试约定。

## 固定时序与数据边界

- 日线事件顺序固定为：交易日开盘处理昨日挂单 → 按未复权价格成交 → 更新现金/持仓 → 收盘估值 → 策略读取截至当日收盘的数据 → 生成下一交易日订单。
- 信号只能读取截至信号日的前复权列；成交、估值、涨跌停判断只能读取未复权列。
- 信号日为 `D` 时最早在 `D+1`开盘成交，严禁使用`D`收盘价或`D+1`收盘后才知道的数据。
- 输入数据按`symbol, trade_date`稳定排序；重复键、缺失必需列或非有限价格必须在运行前拒绝。
- Phase 2不处理分红、送股、拆并股和配股；报告必须列为限制，不能用前复权价格充当成交价。

## 公共接口与模型

在`src/ashare_quant/backtest/`建立独立包，至少包含：

- `Side`: `BUY`、`SELL`。
- `OrderStatus`: `PENDING`、`FILLED`、`REJECTED`、`CANCELLED`。
- `RejectReason`: `INSUFFICIENT_CASH`、`INSUFFICIENT_POSITION`、`T_PLUS_ONE`、`LOT_SIZE`、`SUSPENDED`、`LIMIT_UP`、`LIMIT_DOWN`、`MISSING_BAR`、`INVALID_PRICE`。
- `Signal`: 信号日、symbol、side、请求数量和reason。
- `Order`: 唯一ID、信号信息、计划成交日、状态和拒绝原因。
- `Fill`: 订单ID、成交日、symbol、side、数量、原始开盘价、滑点后价格、佣金、印花税、过户费、总费用和现金变化。
- `Position`: symbol、总数量、可卖数量、当日买入冻结数量、平均未复权成本。
- `PortfolioSnapshot`: 日期、现金、持仓市值、总权益、当日收益、累计收益和回撤。
- `BacktestResult`: 配置摘要、订单流水、成交流水、每日权益、最终持仓、指标和限制说明。

统一接口：

- `Strategy.on_close(context) -> list[Signal]`，不允许访问未来切片。
- `UniverseFilter.is_eligible(symbol, date, context) -> decision`。
- `RiskManager.validate(signal, portfolio, bar, config) -> decision`。
- `BrokerSimulator.execute(order, bar, portfolio, config) -> Fill | rejection`。
- `BacktestEngine.run(data, strategy, start_date, end_date, initial_cash) -> BacktestResult`。

公共模型使用类型注解；金额使用`Decimal`或整数分累计，不得用二进制浮点直接累计现金与费用。报告金额保留两位，价格内部至少四位精度。

## A股成交规则

全部参数写入`config/backtest.default.yaml`，代码不得写死费率：

- 初始资金默认1000元。
- 买入数量必须为100股整数倍；不合法直接拒绝，不自动调整。
- 卖出不得超过可卖数量；当日买入数量当日不可卖，下一交易日解冻。
- 仅支持下一交易日开盘市价撮合；不支持限价单、部分成交和盘中撮合。
- 买入滑点向上、卖出滑点向下，使用可配置bps；滑点后按0.01元tick向不利方向取整。
- 佣金按成交额乘费率并应用最低佣金，买卖双向收取。
- 印花税仅卖出收取；过户费按配置双向收取。费率必须允许设为0。
- 买入现金需求为成交额加买方费用，现金不足整单拒绝；卖出现金增加为成交额减卖方费用。
- `is_suspended=True`或`is_tradable=False`时拒绝。
- 涨跌停价由前收盘未复权价、板块/状态比例和0.01元tick计算；参数与舍入规则写入配置。保守撮合：开盘达到涨停拒绝买入，达到跌停拒绝卖出。
- 首个无前收盘日只执行其他校验，并标记`limit_check_unavailable`。
- 缺失下一交易日bar的挂单在期末标为`CANCELLED`，不得静默丢失。

## 股票池与风控接口

默认`UniverseFilter`只实现上市/退市区间、ST、停牌、有效价格和100股最低购买金额过滤；120日上市期、流动性和趋势过滤留到Phase 3。

默认`RiskManager`检查日期/symbol、正整数及100股数量、单symbol持仓开关（默认开启）、最大持仓市值比例（默认100%）、现金、可卖数量、T+1、停牌、涨跌停和缺失bar。无法购买一手时必须拒绝并保留现金。

## 指标与报告

生成JSON和Markdown，至少包含：初始/最终权益、总收益、年化收益、最大回撤、交易次数、胜率、盈亏比、换手率、不可成交订单率、拒绝原因计数、每日现金/市值/权益、配置摘要、数据范围、内容哈希、代码提交号和限制声明。

每日必须满足`总权益 = 现金 + 持仓市值`。零交易、单交易日、全程现金和期末仍持仓也必须生成有效报告；无法定义的指标使用`null`。

## 必须测试

所有测试离线运行，至少包含：

1. `D`收盘信号只能在`D+1`开盘成交，防未来函数。
2. 100股、200股成功；99股和150股因`LOT_SIZE`拒绝。
3. 1000元在最低佣金后现金不足时整单拒绝且现金不为负。
4. 买入后当日卖出因T+1拒绝，下一交易日可卖。
5. 停牌买卖均拒绝。
6. 涨停买入、跌停卖出拒绝；涨停卖出、跌停买入允许。
7. 滑点方向、tick舍入、最低佣金、卖出印花税和过户费人工核算。
8. 多次买入平均成本、部分卖出、清仓现金与已实现盈亏。
9. 缺少bar、无效价格、期末挂单取消。
10. 单持仓限制、持仓比例限制和全程现金。
11. 每日账务恒等式、ID唯一、相同输入重复运行一致。
12. 报告指标可由订单/成交/权益原始记录独立复算。

提供不超过10个交易日、两只股票的人工金标准例子，逐日列出信号、订单、成交、费用、现金、持仓和权益；测试逐项断言。

## CLI与交付物

新增：

```text
ashare-quant backtest --quotes <curated.parquet> --config <yaml> --strategy scripted --signals <json> --output <dir>
```

数据质量不合格、配置无效或账务不平时返回非零；成功输出`backtest-result.json`、`backtest-report.md`、`orders.parquet`、`fills.parquet`和`equity.parquet`。

交付回测包、默认配置、CLI、测试、金标准样本、`docs/phase-2-backtester.md`和`reports/phase-2/`生成报告。README可补充Phase 2命令，但不得宣称可实盘。

## 完成条件

- Python 3.11完整测试，旧Phase 1测试继续通过。
- 总覆盖率不低于85%，回测核心包不低于90%。
- 实际运行金标准CLI、失败CLI、`git diff --check`和安全扫描。
- 创建并推送`trae/phase-2-backtester`，创建PR但不要合并。
- 最终回复给出命令、退出码、测试统计、覆盖率、报告、提交号和限制。
- 完成后停止等待Codex Gate 2审核，不得开始Phase 3。

