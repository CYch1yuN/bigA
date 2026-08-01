# Gate 4A 第二轮复验报告（14 项）

分支 `workbuddy/phase-4-automation` → PR #7（base=main，**不得合并**）
复验日期：2026-08-02（本地）

## 提交关系（研究代码提交与文档提交分开记录）

- **code_commit（功能代码最后提交）**：`681ea55`（feat(phase-4): complete FR-23 audit artifact set）
- **document_commit（复验报告所在文档提交）**：
  - `544f20a`（初版 14 项复验报告）
  - `d69d22a`（1329 计数同步版）
  - 本最终修订（1332 计数 + 完整周研究证据闭环）由后续文档提交承载
- 注意：`681ea55` 是**功能代码**最后提交，**不是**当前 PR HEAD；当前 HEAD 是承载本报告的文档提交。
  不得将功能提交与文档提交混为一谈。

## 0. Gate 4A 最终复审（PASS WITH FIXES）修复项

阻断项为「正式周示例未真正执行完整研究」——已修复：

- 研究数据升级为 **5 年合成数据（2017-01-02 ~ 2021-12-31，10770 行，8 标的）**，
  满足 `WalkForwardConfig(min_total_years=5)` 的滚动验证要求。
- 重新以 CLI `automation weekly --synthetic` 运行正式周任务，**实际完成**：
  - 样本外折 **folds = 2**（2017-2019 训练→2020 测试；2018-2020 训练→2021 测试）
  - 稳健轨 **81 个**、激进轨 **729 个**参数组合的参数搜索
  - **MC-10000** 已执行：`monte_carlo = {n_paths: 10000, prob_ten_x, prob_loss_50, prob_near_zero}`
  - 压力测试（steady / aggressive）与参数扰动（steady 81 / aggressive 729 组合）均为**非空实测结果**
  - 全流程耗时约 8.5 分钟，`insufficient_sample = False`
- 新增完整性守卫测试（`TestResearchCompletenessGuard` + `test_official_weekly_example_has_complete_research`）：
  仅凭 `candidate_counts` 而 `folds=0 / monte_carlo 为空` 的短路运行，一律不得描述为完整研究。

## 1. 全量测试数量

- `compileall -q src`：**exit 0**
- `pytest --collect-only -q tests`：**1332 项**（Codex 口径 1327 + 后续新增 5 项：
  ① 复验报告措辞/乱码断言 ② manifest 自引用回归测试 ③④ 研究完整性守卫（短路不得冒充完整/完整须满足守卫）
  ⑤ 正式周示例必须含完整研究断言；权威计数以 collect-only 为准）
- `pytest tests -q`：**1332 passed / 0 failed / exit 0**
- 关键 FR 测试集（FR-19/20/21/22/23/24/25 + phase4）：全绿

## 2. 总覆盖率

- `coverage run --source=ashare_quant -m pytest tests` + `coverage report`
- **TOTAL = 91%**（8620 语句 / 799 未覆盖），门槛 ≥90% ✅

## 3. automation 包覆盖率

- `ashare_quant.automation` = **(3964−352)/3964 = 91.12%**，门槛 ≥90% ✅
- （新增 `audit.py` 87%；`cli.py` 60% 为最大缺口，已由离线入口测试覆盖，非慢速管线）

## 4. 真实每日端到端测试

- `tests/test_phase4_automation.py::test_daily_pipeline_success`
- 合成行情 + 注入数据源，完整 10 步每日管线 → `SUCCESS`

## 5. 真实每周 ResearchRunner 测试

- `tests/test_fr19_weekly_research.py::TestRealResearchRunner`（5 项，module-scoped fixture）
- 使用**真实**（非 mock）Phase 3 `ResearchRunner`，极小候选集压到 1 组合/轨

## 6. 每日产物清单（FR-23 审计产物）

- `run.json`、`daily-report.md`、`run-report.md`、`run-summary.json`、`manifest.json`
- `signals.json`、`simulated-orders.json`、`accounts.json`、`quality.json`
- `signals.parquet`、`orders.parquet`、`fills.parquet`、`account-snapshot.json`、`equity.parquet`、`quality-summary.json`
- `latest-daily.md`（全部写完并校验后原子更新，仅 SUCCESS）
- 数据更新器启用时另有 `data-update.json`

## 7. 周研究参数 81/729（已实测完成）

- `weekly_research.py`：`STEADY_PARAM_CANDIDATES` → 81 组合，`AGGRESSIVE_PARAM_CANDIDATES` → 729 组合（默认完整网格）
- 正式周示例实际完成 81 + 729 参数搜索（`candidate_counts = {steady: 81, aggressive: 729}`，
  与 perturbation 实测 `total_combinations` 一致：steady 81 / aggressive 729）

## 8. MC 路径 10000（已实测执行）

- `weekly_research.py::DEFAULT_MONTE_CARLO_CONFIG = MonteCarloConfig(n_paths=10_000, ...)`
- 正式周示例 `monte_carlo = {n_paths: 10000, prob_ten_x: 0.0, prob_loss_50: 0.0, prob_near_zero: 0.0}`
  （10,000 条路径已执行并产出概率分布）

## 9. 主备数据源回退测试

- `tests/test_fr20_data_update.py::test_primary_failure_falls_back_to_baostock`
- `tests/test_fr20_data_update.py::test_fallback_recorded_in_manifest_and_notes`
- 主源失败→备源成功：`fallback_used=True` 如实留痕（manifest + notes）

## 10. SUCCESS 强制重跑阻断测试

- `tests/test_fr25_force_retry.py::test_success_is_never_force_retryable`
- `tests/test_fr25_force_retry.py::test_force_retry_rejected_for_disallowed_states`（含 SUCCESS）
- `tests/test_fr25_force_retry.py::test_fingerprint_change_cannot_bypass_force_retry_gate`
- 指纹变化不得成为绕过闸门的后门

## 11. 双跑一致性

- `tests/test_fr23_audit_artifacts.py::test_deterministic_double_run`
- 同输入双跑（固定注入时钟）：`run.json / run-summary.json / run-report.md / daily-report.md / manifest.json / account-snapshot.json / quality-summary.json / signals.json / simulated-orders.json / accounts.json / quality.json / signals/orders/fills/equity.parquet` **字节一致**
- `run_id` 与 `order_id` 一致（订单与成交经 `order_id` 关联）

## 12. 编码扫描结果

- 扫描范围：docs/**/*.md、config/**/*.yaml、config/**/*.yml、scripts/*.ps1、src/ashare_quant/automation/*.py、tests/test_fr*.py、tests/test_phase4_automation.py、reports/phase-4/**
- **files=79 / garble=0 / utf8-decode-fail=0 / json-parse-fail=0**（四类均为 0）
- `tests/test_fr22_encoding.py` 锁定该保证（含合成每日/每周报告生成的 UTF-8 校验）

## 13. 密钥扫描结果

- 扫描 204 个文本文件：候选 12 处，**全部为 reports/phase-1~3 中的内容/配置/数据 SHA-256 哈希**（可复现性审计值）
- **0 真实密钥**（无 API key / token / 私钥）

## 14. git diff --check

- `git diff --check origin/main..HEAD`：**exit 0**（无空白错误）

## 15. Gate 4B 真实观察跟踪器修复（PASS WITH FIXES → 修复完成）

复审阻断项：`_track_real` 用 `(d - prev).days == 1` 按**自然日**判断连续性，
周五→周一相差 3 天会每周重置计数，真实 A 股观察窗口永远无法达到 60 天；
且 `track` 只统计 SUCCESS 记录，未复核缺失日/失败日/重复订单/恒等式/负现金/
manifest 可复算性。

修复（`scripts/gate4b_observation.py` + `tests/test_gate4b_tracker.py`，12 项测试）：

- **交易日历驱动**：自启动日（最早运行记录）起用 `TradingCalendar.next_trading_day`
  生成预期交易日序列，周末/节假日由日历跳过，**不再按自然日推断**；
- **逐日复核**（每一预期交易日全部满足才计入连续段）：
  1. 运行记录存在且 `SUCCESS` / exit 0（缺失或失败即违规）；
  2. 报告产物齐全（run.json / manifest.json / accounts.json /
     simulated-orders.json / signals.json）；
  3. `manifest.json` 哈希可复算（`verify_manifest`）；
  4. 全窗口订单 `unique_key` 与 `order_id` **各自唯一**（两个独立集合，
     避免跨字段偶然同值误报；无重复订单）；
  5. 账务恒等式 `cash + position_value == total_equity`（无无法解释的权益变化），
     现金非负；
- **fail-closed**：交易日历缺失 → `calendar_error` + 进度 0，不产出虚假进度；
  日历覆盖不足 60 个交易日 → 如实标记 `calendar_coverage`，不假装达标；
- **启动日归一化**：最早运行记录可能是节假日/周末产生的
  `SKIPPED_NON_TRADING_DAY` 记录（非交易日）——它**不是**预期交易日，若直接
  以它为起点会因「非 SUCCESS」永久停在 0。规则：最早运行日期当天是交易日
  → 用当天；否则 → `calendar.next_trading_day(earliest, inclusive=True)`
  取其后最近交易日；
- 只有从启动日起**连续 60 个预期交易日全部满足**验收条件才输出 60/60。
- 回归测试覆盖：周末不重置（60 个工作日全通过 → 60/60）、首日非周一、
  首条记录为节假日 SKIPPED 时归一化起点（→ 60/60）、仅节假日记录无 SUCCESS
  → 0/60 且非交易日记录不判违规、缺失日/失败日/重复订单/恒等式违规/负现金/
  manifest 篡改各自中断、跨字段同值不误报（unique_key 与 order_id 独立集合）、
  无记录 NOT STARTED、日历不可用 fail-closed、日历覆盖不足、Markdown 渲染口径。

## 16. 全量测试数量（tracker 修复后）

- `pytest --collect-only -q tests`：**1349 项**（1334 + 12 项 tracker 回归 +
  3 项启动边界/独立集合回归）
- `pytest tests -q`：**1349 passed / 0 failed / exit 0**
- 覆盖率：**TOTAL 91%**（8620/796）、**automation 91.20%**（3964/349），双门槛 ≥90% ✅

---

## 结论

FR-19/20/21/22/23/24/25 全部完成；全量测试 1349 全绿；双门槛覆盖率达标
（TOTAL 91%、automation 91.20%）；编码与密钥扫描零问题；双跑字节一致；
正式周示例已完成完整研究（folds=2、81/729 参数搜索、MC-10000、压力与扰动实测结果）。
Gate 4A 复审阻断项已闭环；Gate 4B 真实观察跟踪器已修复（交易日历驱动 + 逐日复核 +
启动日归一化 + unique_key/order_id 独立集合）。

Gate 4B 状态口径：
- **historical replay（60 日历史回放预检）：PASS**
- **continuous operation（连续 60 个交易日自动运行）：NOT STARTED（0/60）**
  （从真实自动任务启动日起按交易日历累计，未启动；tracker 已就绪）
- **实盘资格：不具备**（两轨结论不变）

**等待 Codex Gate 4B 复审（continuous operation 尚未开始）。**
