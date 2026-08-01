# Phase 4 本机自动化研究与模拟运行系统

## 概述

Phase 4 在 Phase 1（数据管线）、Phase 2（回测器）、Phase 3（双轨策略研究）之上，把"研究信号 + 模拟账户"这条链路固化成**可每日/每周在本机无人值守跑批**的自动化系统。它像一台装了定时器的织布机：每天收盘后自动织出研究信号，每周六把一周的布匹收拢成一份可审计的账。

**本阶段的全部产出都是研究信号与模拟账户记录，不连接券商、不发送真实订单、不接触任何真实资金。** 这是一条不可逾越的红线，由资格闸门与 `live_trading` 永久关闭双重锁死。

## 安全边界（不可逾越）

- 本系统只产出**研究信号**、**模拟订单**与**模拟账户报告**。
- 禁止连接券商接口，禁止接触任何真实资金账户。
- 稳健轨资格结论固定为 `NOT_ELIGIBLE_FOR_LIVE_TRADING`，激进轨固定为 `SIMULATION_ONLY`，两者**均未获得实盘授权**。
- `live_trading.enabled` 即便被改成 `true`，也会被资格闸门拦截为 `BLOCKED_NOT_ELIGIBLE` 并以非零码退出——它永远不会真的去下单。
- 模拟订单与待成交信号文件**不含任何券商下单字段**，无法直接导入交易终端；不提供"一键实盘"能力。

> 边界声明会原样印在每一份 `daily-report.md` / `weekly-report.md` 顶部，确保任何读到报告的人第一眼就看到这道墙。

## 架构

```
src/ashare_quant/automation/
├── cli.py              # `ashare-quant automation <daily|weekly|status|verify|rerun|install|uninstall>`
├── config.py           # 配置加载（config/automation.default.yaml），路径/阈值集中于此
├── runner.py           # 运行编排器：恢复中断→指纹→幂等→加锁→执行→收尾（终态报告在终态后渲染）
├── models.py           # 状态机、RunRecord、StepResult、各类阻断异常
├── locking.py          # 跨进程原子锁（Windows OpenProcess 探活，孤儿锁接管，释放重试确认）
├── state.py            # 状态仓库：运行记录、模拟账户、待成交信号（幂等依据）
├── calendar.py         # fail-closed 交易日历加载（缺失/过期一律失败，不按工作日推断）
├── datasource.py       # 行情数据源抽象（InjectedDataSource 注入合成样本用于离线验证）
├── quality.py 引用     # 复用 Phase 1 质量闸门
├── daily.py            # 每日管线（10 步）与双轨模拟账户撮合
├── weekly.py           # 每周管线（8 步）只读复盘（不盯市、不撮合、不改账户）
├── simulated_account.py# 纸面账户管理（paper-steady / paper-aggressive）
├── reporting.py        # run.json / Markdown 报告渲染、ResultPaths 推导
├── alerts.py           # 本机告警（latest-failure.md/json、可选 webhook）
├── archive.py          # 历史结果归档与保留策略
├── scheduler.py        # 渲染 Windows `schtasks` 命令（不依赖 Windows 即可单测）
└── idempotency.py      # 输入/配置/代码指纹，决定能否复用既有成功记录

scripts/                       # Windows 任务计划调用的 PowerShell 包装
├── run_daily.ps1
├── run_weekly.ps1
├── install_scheduler.ps1
└── uninstall_scheduler.ps1
```

配置文件：`config/automation.default.yaml`，所有路径、阈值、时间窗口集中于此，代码中禁止硬编码。

## 9 态状态机

每次运行都收敛到下列 9 个终态之一；任何未预期异常都落到 `FAILED`（绝不静默判成功）。

| 终态 | 含义 | 退出码 |
| --- | --- | --- |
| `PENDING` / `RUNNING` | 仅运行中内部态，不作为终态落盘 | — |
| `SUCCESS` | 跑批成功，产物已落盘 | 0 |
| `SKIPPED_NON_TRADING_DAY` | 业务日非交易日 | 0 |
| `SKIPPED_DATA_UNAVAILABLE` | 行情不可用 | 0 |
| `BLOCKED_DATA_QUALITY` | 质量闸门 critical>0 阻断 | 3 |
| `BLOCKED_LOCKED` | 另一实例持有活跃锁 | 4 |
| `BLOCKED_NOT_ELIGIBLE` | 资格闸门拦截（含误开实盘开关） | 5 |
| `FAILED` | 其它任何异常，或**交易日历不可用（fail-closed）** | 1 |

`runner.py` 顶部有一张"异常 → 终态"映射表，顺序敏感（子类在前），未知异常一律 `FAILED`。

## fail-closed 交易日历

`load_trading_calendar` 在加载时执行 `assert_fresh(as_of, max_staleness_days=30)`：
若日历文件缺失、超出覆盖范围、或距生成日超过 30 天，直接抛 `CalendarUnavailableError` → `FAILED`。

**关键语义：无法确认今天是不是交易日，就没有资格宣称"今天没什么可做的"。** 因此"日期落在日历覆盖范围之外"与"今天是非交易日"是两种截然不同的结局——前者是失败（fail-closed），后者是正常跳过。不要把它俩混为一谈。

## 跨进程锁

`RunLock` 用 `os.open(O_CREAT | O_EXCL | O_WRONLY)` 原子创建锁文件，保证同一机器同一任务同一业务日只有一个实例在跑。

- 探活用 Windows `OpenProcess` + `GetExitCodeProcess`，**绝不**用 `os.kill`（Windows 上 `os.kill` 语义不同，会误杀）。
- 陈旧阈值 `stale_after_seconds = 21600`（6 小时）。
- **孤儿锁接管**：若持锁进程 pid 与当前进程相同但 `run_id` 不同（本进程残留在上一次运行中的锁），直接判定为孤儿锁并接管，避免"自己把自己锁死"。
- **释放重试确认**：`release()` 删除锁文件后确认其确实消失；若遇到 Defender/索引服务短暂持句柄导致的 `PermissionError`，重试若干次；仍失败则记录 `release_error` 并显式告警，绝不静默吞掉（否则下次运行会被误判为"另一实例正在运行"）。

## 数据质量闸门

复用 Phase 1 的质量检查（`QualityChecker`）。`critical > 0` 一律阻断为 `BLOCKED_DATA_QUALITY`；可配置 `max_warning` 上限。闸门**不允许**静默复用昨日数据（`allow_stale_fallback` 固定为 `false`）——fail-closed 原则同样适用于数据缺口。

## 模拟账户与 60 交易日观察窗口

- 两个纸面账户：`paper-steady`（稳健轨，`NOT_ELIGIBLE_FOR_LIVE_TRADING`）、`paper-aggressive`（激进轨，`SIMULATION_ONLY`）。
- 双轨策略在 **D 日收盘后**生成信号，最早在 **D+1 开盘** 撮合；当日只把信号写回 `pending-signals.json` 等次日成交，绝不就地假装成交（无未来函数）。
- 60 交易日观察窗口：每日管线推进 `observation_days`，跑满 60 个交易日**不等于**取得实盘资格——窗口只是研究观察期。
- 每周管线对账户**只读**：不盯市、不撮合、不改账户文件，否则一周会凭空多出一个"观察日"，悄悄注水窗口。

## CLI 用法

所有子命令挂在主入口的 `automation` 组下：

```bash
# 每日跑批（默认业务日=今天）
ashare-quant automation daily [--date YYYY-MM-DD] [--config PATH] [--dry-run] [--synthetic]

# 每周汇总（默认业务日=今天；通常周六跑）
ashare-quant automation weekly [--date YYYY-MM-DD] [--config PATH] [--dry-run] [--synthetic]

# 查看最近运行与模拟账户状态
ashare-quant automation status [--config PATH]

# 校验安全边界与配置可行性（不真正跑批）
ashare-quant automation verify [--config PATH] [--synthetic]

# 强制重跑某次运行（attempt 递增，绕过幂等复用）
ashare-quant automation rerun --task {daily,weekly} --date YYYY-MM-DD [--config PATH] [--synthetic]

# 注册 / 注销 Windows 任务计划（调用 scripts/*.ps1）
ashare-quant automation install  [--config PATH] [--task-prefix PREFIX] [--force] [--dry-run] [--yes]
ashare-quant automation uninstall [--config PATH] [--task-prefix PREFIX] [--dry-run] [--yes]
```

`--synthetic` 注入合成行情与日历，用于本机离线验证（不触碰任何真实在线数据源，也不伪造在线抓取成功）。`--dry-run` 只演练不落盘（账户状态与报告均不写）。

## Windows 任务计划

`scheduler.py` 只负责**渲染** `schtasks` 命令，不依赖 Windows 即可单测（dry-run 友好）：

- 每日任务：默认 `18:30` 触发，调用 `run_daily.ps1`（与 `data.ready_time` 同一时刻，数据未就绪时由管线内重试兜底）。
- 每周任务：默认周六 `09:00` 触发，调用 `run_weekly.ps1`。
- 运行级别 `LIMITED`，工作目录为仓库根。
- `install` / `uninstall` 子命令把渲染好的命令交给 `scripts/install_scheduler.ps1` / `uninstall_scheduler.ps1` 经 PowerShell 实际注册（需 `--yes` 确认执行，否则只打印）。

## 产物与报告

每次运行落盘到 `reports/phase-4/`，每日按日期分目录、每周按 ISO 周分目录（重跑同一天/同一周覆盖同一目录，天然幂等，不堆时间戳垃圾）：

```
reports/phase-4/
├── daily/<YYYY-MM-DD>/
│   ├── run.json              # 运行记录（状态机、步骤、指纹）——终态确定后才落盘
│   ├── daily-report.md       # 人类可读报告（顶部含边界声明）
│   ├── signals.json          # 研究信号（非投资建议、非交易指令）
│   ├── simulated-orders.json # 模拟订单（纸面撮合结果）
│   ├── accounts.json         # 模拟账户快照
│   └── quality.json          # 数据质量闸门结果
├── weekly/<ISO-WEEK>/
│   ├── run.json
│   ├── weekly-report.md
│   ├── signals.json / simulated-orders.json / accounts.json
│   └── weekly-summary.json   # 周度聚合（覆盖率、缺口、观察进度、资格闸门）
├── latest-daily.md           # 最近一次每日报告软链式副本
└── latest-weekly.md          # 最近一次每周报告软链式副本
```

> 示例产物（合成行情，ISO 周 2020-W40，2020-09-28 ~ 2020-10-02 + 周六 2020-10-03）已生成于 `../reports/phase-4/`：
> - 每日报告：`../reports/phase-4/daily/2020-10-02/daily-report.md`
> - 周报：`../reports/phase-4/weekly/2020-W40/weekly-report.md`
>
> 报告顶部"终态"行应显示 **成功 (`SUCCESS`)**、退出码 `0`、结束时间已填充——这是本阶段修复的关键正确性点（见下）。

## 幂等性

- `runner.build_fingerprint` 对代码提交、配置哈希、输入行情哈希、业务日、标的、回看天数联合哈希，得到确定性 `run_id`。
- 该业务日已有 `SUCCESS` 记录时直接复用，不重复扣款、不重复写报告。
- **指纹变化也不重跑成功记录**：改配置或换 commit 会让 `run_id` 变化，但该业务日的模拟成交与观察窗口计数已经落账，重算即二次记账，因此仍然复用既有结果（原因码 `idempotent_reuse_fingerprint_changed`）。

### `--force-retry` 的适用边界（FR-25）

`--force-retry` 不是万能钥匙，只对"确实需要重试"的既有终态放行：

| 既有终态 | `--force-retry` | 理由 |
| --- | --- | --- |
| `FAILED` | ✅ 允许 | 运行没跑完；中断恢复判定出的 `FAILED` 同样适用 |
| `SKIPPED_DATA_UNAVAILABLE` | ✅ 允许 | 数据源当时不可用，补数后重试正当 |
| `BLOCKED_DATA_QUALITY` | ✅ 允许 | 质量闸门当时拦截，修数后重试正当 |
| `SUCCESS` | ❌ 拒绝 | 会二次改写资金与观察窗口，属审计事故 |
| `SKIPPED_NON_TRADING_DAY` | ❌ 拒绝 | 不是交易日，重跑不会有新结论 |
| `BLOCKED_LOCKED` | ❌ 拒绝 | 另一实例在跑，正确处置是等待而非抢跑 |
| `BLOCKED_NOT_ELIGIBLE` | ❌ 拒绝 | 不得用于绕过安全边界 |

适用性判定发生在**指纹比较之前**——改配置或换 commit 不能成为绕过闸门的后门。被拒绝时 CLI 打印原因码 `force_retry_not_applicable` 并原样返回既有记录，**模拟账户、观察窗口与模拟订单一律不动**。

被拒绝的终态并非无法重跑：它们本就不是 `SUCCESS`，常规 `daily` / `weekly` 路径不会命中幂等复用分支，直接再跑一次即可。

## 已知缺陷与修复（本阶段）

1. **幽灵锁（已修复）**：在 `Documents/` 这类被 Defender/索引服务监视的目录上，`release()` 删除锁文件后可能因句柄未释放而失败；旧实现 `except OSError: return False` 把失败静默吞掉，残留锁导致下一次运行被自家进程判为 `BLOCKED_LOCKED`，形成"自己锁死自己"的死循环。修复：`locking.py` 增加释放重试+确认，并新增"本进程孤儿锁接管"判定；`runner.py` 在释放失败时显式告警。
2. **报告终态错乱（已修复）**：`run.json` 与 Markdown 报告原在 `artifacts` 步骤中渲染，彼时 `ctx.record.state` 仍是 `RUNNING`，导致 SUCCESS 运行的报告显示"运行中（非终态）/ 退出码 1 / 结束时间 —"。修复：将数据产物（信号/订单/账户/质量）与终态报告（run.json + 报告）拆开，终态报告延迟到 `runner.py` 设置终态**之后**再渲染，由业务管线以 `_finalize_report` 回调注册、`runner` 在收尾时调用。

## Gate 4A 验收要点

- 每日管线 10 步、每周管线 8 步全部存在且顺序正确。
- 9 态状态机完整，`FAILED` 为未知异常的兜底；交易日历不可用为 `FAILED`（非跳过）。
- 跨进程锁在 Windows 上原子、可探活、可接管孤儿锁、释放失败可告警。
- 数据质量闸门 fail-closed，不静默复用昨日数据。
- 模拟账户只读由周报保证；观察窗口不被周报注水。
- 报告终态与真实运行结果一致（不得出现 RUNNING/退出码 1 的"假运行中"报告）。
- ≥25 项自动化测试通过（当前 39 项，覆盖状态机、锁、幂等、日历、质量、账户、归档、CLI）。
- 安全边界不可被配置绕过；误开 `live_trading.enabled` 必须被 `BLOCKED_NOT_ELIGIBLE` 拦截。
```
