# 每周自动化汇总报告 · 2020-W40

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
| 运行标识 | `weekly-20201003-b309d2d6dafa4923` |
| 任务类型 | weekly |
| 业务日 | 2020-10-03 |
| 终态 | **成功** (`SUCCESS`) |
| 退出码 | 0 |
| 尝试次数 | 1 |
| 代码提交 | `d69d22a40e26ee807e401a331b4eaa5be9ec58f3` |
| 配置哈希 | `407d5f82c815276b` |
| 输入哈希 | `35bc76fb825ecd69` |
| 开始时间 | 2026-08-02 01:40:57.105977 |
| 结束时间 | 2026-08-02 01:49:23.445781 |
| 结论 | 每周汇总完成：2020-W40 覆盖 5/5 个交易日，模拟成交 0 笔，新增研究信号 1 条，观察窗口 5/60 交易日，归档 0 个目录；全部为模拟账户记录，未连接券商、未涉及真实资金 |

## 步骤明细

| # | 步骤 | 状态 | 耗时(s) | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `lock` | OK | 0.002006 | acquired=True, reason=成功获取锁, stole_stale=False, holder=7项 |
| 2 | `preflight` | OK | 0.001 | accounts=2项, live_trading_enabled=False, broker_connected=False, mutates_account_state=False |
| 3 | `calendar` | OK | 0.001 | source=synthetic-calendar, first_date=2020-01-02, last_date=2020-10-07, trading_day_count=5 |
| 4 | `collect_runs` | OK | 0.003063 | expected_trading_days=5, records_found=5, signals=1, orders=0 |
| 5 | `coverage_audit` | OK | 0.0 | expected_trading_days=5, succeeded_days=5, missing_days=0项, unhealthy_days=0项 |
| 6 | `weekly_research` | OK | 506.292739 | ran=True, skipped_reason=None, error=None, insufficient_sample=False |
| 7 | `account_review` | OK | 0.001003 | accounts=2, uninitialized=0项, weekly_return_pct=2项, state_written=False |
| 8 | `observation_review` | OK | 0.0 | accounts_reviewed=2, observation_completed=0项, live_trading_authorized=False, requires_independent_review=True |
| 9 | `archive` | OK | 0.001462 | enabled=True, archived_count=0, archived_bytes=0, pruned_batches=0项 |
| 10 | `artifacts` | OK | 0.021838 | files=4项, dry_run=False |

## 本周每日运行回顾

| 业务日 | 终态 | 退出码 | 尝试 | 结论 |
| --- | --- | --- | --- | --- |
| 2020-09-28 | 成功 | 0 | 1 | 每日模拟运行完成：撮合 0 笔（共 0 条订单记录），新增研究信号 0 条，观察窗口 1/60 交易日；全部为模拟账户记录，未连接券商、未涉及真实资金 |
| 2020-09-29 | 成功 | 0 | 1 | 每日模拟运行完成：撮合 0 笔（共 0 条订单记录），新增研究信号 0 条，观察窗口 2/60 交易日；全部为模拟账户记录，未连接券商、未涉及真实资金 |
| 2020-09-30 | 成功 | 0 | 1 | 每日模拟运行完成：撮合 0 笔（共 0 条订单记录），新增研究信号 0 条，观察窗口 3/60 交易日；全部为模拟账户记录，未连接券商、未涉及真实资金 |
| 2020-10-01 | 成功 | 0 | 1 | 每日模拟运行完成：撮合 0 笔（共 0 条订单记录），新增研究信号 0 条，观察窗口 4/60 交易日；全部为模拟账户记录，未连接券商、未涉及真实资金 |
| 2020-10-02 | 成功 | 0 | 1 | 每日模拟运行完成：撮合 0 笔（共 0 条订单记录），新增研究信号 1 条，观察窗口 5/60 交易日；全部为模拟账户记录，未连接券商、未涉及真实资金 |

## 本周统计

| 指标 | 值 |
| --- | --- |
| ISO 周 | 2020-W40 |
| 应跑交易日 | 5 |
| 成功跑批 | 5 |
| 缺失跑批 | 0 |
| 异常跑批 | 0 |
| 跑批覆盖率 | 100.0% |
| 新增研究信号 | 1 |
| 模拟订单记录 | 0 |
| 模拟成交 | 0 |
| 模拟拒单 | 0 |
| paper-steady 周收益率 | 0.0% |
| paper-steady 最大回撤 | 0.0% |
| paper-aggressive 周收益率 | 0.0% |
| paper-aggressive 最大回撤 | 0.0% |

## 模拟账户

| 账户 | 轨道 | 资格结论 | 现金 | 持仓市值 | 总权益 | 持仓数 | 已观察交易日 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| paper-steady | steady | `NOT_ELIGIBLE_FOR_LIVE_TRADING` | 1000.00 | 0.00 | 1000.00 | 0 | 5 |
| paper-aggressive | aggressive | `SIMULATION_ONLY` | 1000.00 | 0.00 | 1000.00 | 0 | 5 |

## 60 交易日观察窗口

| 账户 | 资格结论 | 已观察 | 目标 | 剩余 | 进度 | 是否完成 |
| --- | --- | --- | --- | --- | --- | --- |
| paper-steady | `NOT_ELIGIBLE_FOR_LIVE_TRADING` | 5 | 60 | 55 | 8.33% | 否 |
| paper-aggressive | `SIMULATION_ONLY` | 5 | 60 | 55 | 8.33% | 否 |

> 观察窗口完成**不等于**获得实盘资格。窗口结束后仍需独立复审，且复审结论不由本自动化系统作出。

## 归档

| 项目 | 值 |
| --- | --- |
| 归档开关 | 启用 |
| 演练模式 | 否 |
| 批次 | 2020-W40 |
| 保留天数 | 180 |
| 归档截止日 | 2020-04-06 |
| 本次归档目录数 | 0 |
| 本次归档字节数 | 0 |
| 清理批次数 | 0 |
| 清理文件数 | 0 |
| 备注 | 无过期结果（保留 180 天，截止 2020-04-06） |

## 产物清单

- `reports/phase-4/weekly/2020-W40/signals.json`
- `reports/phase-4/weekly/2020-W40/simulated-orders.json`
- `reports/phase-4/weekly/2020-W40/accounts.json`
- `reports/phase-4/weekly/2020-W40/weekly-summary.json`

## 跑批缺口

本周 5 个交易日全部成功跑批，无缺口。
