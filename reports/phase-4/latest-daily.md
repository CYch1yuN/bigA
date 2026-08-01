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
| 运行标识 | `daily-20201002-5371239a4d71421b` |
| 任务类型 | daily |
| 业务日 | 2020-10-02 |
| 终态 | **成功** (`SUCCESS`) |
| 退出码 | 0 |
| 尝试次数 | 1 |
| 代码提交 | `544f20aca1d8602cda98fd2676a5cb2695a271f1` |
| 配置哈希 | `407d5f82c815276b` |
| 输入哈希 | `02e0c037a20c1784` |
| 开始时间 | 2026-08-02 01:10:12.841536 |
| 结束时间 | 2026-08-02 01:10:13.059141 |
| 结论 | 每日模拟运行完成：撮合 0 笔（共 0 条订单记录），新增研究信号 1 条，观察窗口 5/60 交易日；全部为模拟账户记录，未连接券商、未涉及真实资金 |

## 步骤明细

| # | 步骤 | 状态 | 耗时(s) | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `lock` | OK | 0.001001 | acquired=True, reason=成功获取锁, stole_stale=False, holder=7项 |
| 2 | `preflight` | OK | 0.000999 | accounts=2项, live_trading_enabled=False, broker_connected=False, eligibility=2项 |
| 3 | `calendar` | OK | 0.001002 | source=synthetic-calendar, first_date=2020-01-02, last_date=2020-10-07, trading_day_count=200 |
| 4 | `market_data` | OK | 0.003031 | source=synthetic-research-samples, online=False, synthetic=True, rows=1160 |
| 5 | `quality_gate` | OK | 0.03276 | critical=0, warning=0, total=0, has_critical=False |
| 6 | `settle_pending` | OK | 0.001 | pending_signal_date=2020-10-02, pending_count=0, filled=0, rejected=0 |
| 7 | `universe` | OK | 0.000504 | symbols=8, security_master_available=True, filter_kwargs=2项 |
| 8 | `generate_signals` | OK | 0.137594 | signal_date=2020-10-02, fill_date=2020-10-05, count=1, by_track=2项 |
| 9 | `stage_pending` | OK | 0.004002 | written=True, count=1, path=pending-signals.json |
| 10 | `mark_to_market` | OK | 0.00551 | priced_symbols=8, accounts=2, persisted=True, observation=2项 |
| 11 | `artifacts` | OK | 0.015736 | files=4项, dry_run=False |

## 数据出处

| 项目 | 值 |
| --- | --- |
| 数据源 | `synthetic-research-samples` (**非线上抓取**、**合成样本**) |
| 行数 | 1160 |
| 标的数 | 8 |
| 覆盖区间 | 2020-03-16 ~ 2020-10-02 |
| 加载时间 | 2026-08-02T01:10:12.850567 |

数据源备注：

- 合成样本数据，仅用于离线测试，不代表真实市场

## 数据质量闸门

| 项目 | 值 |
| --- | --- |
| 严重问题 | **0** |
| 警告 | 0 |
| 检查行数 | 1160 |
| 是否阻断下游 | 否 |

## 研究信号

| 轨道 | 标的 | 方向 | 数量 | 信号日 | 依据 |
| --- | --- | --- | --- | --- | --- |
| — | `000001` | BUY | 100 | 2020-10-02 | 稳健轨: 得分3.2413, 买入000001 |

> 上表为**研究信号**，不是操作建议。信号仅用于推进模拟账户。

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
