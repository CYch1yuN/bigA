# 每日自动化运行报告 · 2020-10-02

> **边界声明**
>
> - 本报告由本机自动化研究系统生成，内容为**研究信号**与**模拟账户**记录。
> - 所有订单均为**纸面模拟**，未连接任何券商、未涉及任何真实资金。
> - 稳健轨结论为 `NOT_ELIGIBLE_FOR_LIVE_TRADING`，激进轨结论为 `SIMULATION_ONLY`；
>   两者**均未获得实盘授权**。
> - 本报告不构成投资建议，不构成买卖推荐，不可作为交易决策依据。


## 运行概览

| 项目 | 值 |
| --- | --- |
| 运行标识 | `daily-20201002-967c9f92f76750e1` |
| 任务类型 | daily |
| 业务日 | 2020-10-02 |
| 终态 | **成功** (`SUCCESS`) |
| 退出码 | 0 |
| 尝试次数 | 1 |
| 代码提交 | `7f1c8927e8f7933e3ca39f7e8ec3c84fd198800d` |
| 配置哈希 | `5abdf662f72f3b6e` |
| 输入哈希 | `731eaf7d346a36d6` |
| 开始时间 | 2026-08-01 18:59:53.651099 |
| 结束时间 | 2026-08-01 18:59:53.786857 |
| 结论 | 每日模拟运行完成：撮合 0 笔（共 0 条订单记录），新增研究信号 0 条，观察窗口 5/60 交易日；全部为模拟账户记录，未连接券商、未涉及真实资金 |

## 步骤明细

| # | 步骤 | 状态 | 耗时(s) | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `lock` | OK | 0.000999 | acquired=True, reason=成功获取锁, stole_stale=False, holder=7项 |
| 2 | `preflight` | OK | 0.0 | accounts=2项, live_trading_enabled=False, broker_connected=False, eligibility=2项 |
| 3 | `calendar` | OK | 0.0 | source=synthetic-calendar, first_date=2020-01-02, last_date=2020-10-07, trading_day_count=201 |
| 4 | `market_data` | OK | 0.001997 | source=synthetic-research-samples, online=False, synthetic=True, rows=1546 |
| 5 | `quality_gate` | OK | 0.032539 | critical=0, warning=0, total=0, has_critical=False |
| 6 | `settle_pending` | OK | 0.000989 | pending_signal_date=2020-10-01, pending_count=0, filled=0, rejected=0 |
| 7 | `universe` | OK | 0.001 | symbols=8, security_master_available=True, filter_kwargs=2项 |
| 8 | `generate_signals` | OK | 0.065593 | signal_date=2020-10-02, fill_date=2020-10-03, count=0, by_track=2项 |
| 9 | `stage_pending` | OK | 0.003042 | written=True, count=0, path=pending-signals.json |
| 10 | `mark_to_market` | OK | 0.003008 | priced_symbols=8, accounts=2, persisted=True, observation=2项 |
| 11 | `artifacts` | OK | 0.010516 | files=4项, dry_run=False |

## 数据出处

| 项目 | 值 |
| --- | --- |
| 数据源 | `synthetic-research-samples` (**非线上抓取**、**合成样本**) |
| 行数 | 1546 |
| 标的数 | 8 |
| 覆盖区间 | 2020-01-02 ~ 2020-10-02 |
| 加载时间 | 2026-08-01T18:59:53.657622 |

数据源备注：

- 合成样本数据，仅用于离线测试，不代表真实市场

## 数据质量闸门

| 项目 | 值 |
| --- | --- |
| 严重问题 | **0** |
| 警告 | 0 |
| 检查行数 | 1546 |
| 是否阻断下游 | 否 |

## 研究信号

本交易日无新增研究信号（含稳健轨 HOLD_CASH 情形）。

## 模拟订单

| 账户 | 轨道 | 标的 | 方向 | 数量 | 状态 | 成交价 | 费用合计 | 现金变动 | 拒因 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | — | — | — | — | 本期无模拟订单 |

## 模拟账户

| 账户 | 轨道 | 资格结论 | 现金 | 持仓市值 | 总权益 | 持仓数 | 已观察交易日 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| paper-steady | steady | `NOT_ELIGIBLE_FOR_LIVE_TRADING` | 1000.00 | 0.00 | 1000.00 | 0 | 5 |
| paper-aggressive | aggressive | `SIMULATION_ONLY` | 1000.00 | 0.00 | 1000.00 | 0 | 5 |

## 观察窗口进度

| 账户 | 已观察 | 目标 | 剩余 | 进度 |
| --- | --- | --- | --- | --- |
| paper-steady | 5 | 60 | 55 | 8.33% |
| paper-aggressive | 5 | 60 | 55 | 8.33% |

## 产物清单

- `reports/phase-4/daily/2020-10-02/signals.json`
- `reports/phase-4/daily/2020-10-02/simulated-orders.json`
- `reports/phase-4/daily/2020-10-02/accounts.json`
- `reports/phase-4/daily/2020-10-02/quality.json`
