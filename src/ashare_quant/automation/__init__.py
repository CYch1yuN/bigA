"""Phase 4：Windows 本机自动化研究与模拟运行系统。

本包提供每日 / 每周自动化任务的编排能力，全部输出均为**研究信号、模拟订单
与模拟账户报告**。

重要安全边界（不可通过配置绕过）：

- 本包 **不是实盘交易授权**。
- 不连接任何券商接口、不接触任何真实资金账户。
- 稳健轨资格结论为 ``NOT_ELIGIBLE_FOR_LIVE_TRADING``；
  激进轨资格结论为 ``SIMULATION_ONLY``。
- 所有报告必须携带资格标记，禁止输出"建议买入"等实盘指令式表述，
  禁止生成券商可直接导入的下单格式，禁止提供一键实盘执行入口。

模块划分::

    models.py             运行状态机、运行记录、模拟订单/账户数据结构
    config.py             AutomationConfig（Pydantic 校验）与加载器
    calendar.py           交易日历（fail-closed，不假设工作日=交易日）
    idempotency.py        run_id / config_hash / input_hash 计算
    locking.py            跨进程文件锁（含陈旧锁检测）
    state.py              运行状态仓库（原子写入）
    logging_setup.py      结构化 JSONL 日志 + 敏感字段脱敏
    alerts.py             本机告警（标记文件 / 最新失败报告 / 可选 webhook）
    datasource.py         行情数据源抽象（可注入、可离线、不可伪造）
    data_update.py        真实数据更新器（复用 Phase 1 抓取/落盘/清单链路）
    simulated_account.py  模拟账户（复用 Phase 2 撮合与风控）
    reporting.py          运行报告与观察窗口报告
    daily.py              每日任务编排（信号生成复用 Phase 3 策略）
    weekly.py             每周任务编排
    archive.py            结果归档与保留策略
    scheduler.py          Windows 任务计划命令生成（支持 dry-run）
    cli.py                automation 子命令入口
    runner.py             状态机驱动器（锁 / 幂等 / 状态落盘的统一入口）
"""
from __future__ import annotations

SCHEMA_VERSION = 1
"""Phase 4 自动化产物 schema 版本。"""

PHASE = "phase-4"
"""报告目录与归档使用的阶段标识。"""

__all__ = ["SCHEMA_VERSION", "PHASE"]
