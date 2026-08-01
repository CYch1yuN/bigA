# Gate 4A 第二轮复验报告（14 项）

分支 `workbuddy/phase-4-automation` → PR #7（base=main，**不得合并**）
复验日期：2026-08-02（本地）

## 提交关系（研究代码提交与文档提交分开记录）

- **code_commit（功能代码最后提交）**：`681ea55`（feat(phase-4): complete FR-23 audit artifact set）
- **document_commit（复验报告所在文档提交）**：`544f20a`（初版）→ 本修订版由后续文档提交承载
- 注意：`681ea55` 是**功能代码**最后提交，**不是**当前 PR HEAD；当前 HEAD 是承载本报告的文档提交。
  不得将功能提交与文档提交混为一谈。

## 1. 全量测试数量

- `compileall -q src`：**exit 0**
- `pytest --collect-only -q tests`：**1327 项**（Codex 独立收集口径，非手写预计值）
- `pytest tests -q`：**1327 passed / 0 failed / exit 0**
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

## 7. 周研究参数 81/729

- `weekly_research.py`：`STEADY_PARAM_CANDIDATES` → 81 组合，`AGGRESSIVE_PARAM_CANDIDATES` → 729 组合（默认完整网格）

## 8. MC 路径 10000

- `weekly_research.py::DEFAULT_MONTE_CARLO_CONFIG = MonteCarloConfig(n_paths=10_000, ...)`

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

---

## 结论

FR-19/20/21/22/23/24/25 全部完成；全量测试 1327 全绿；双门槛覆盖率达标
（TOTAL 91%、automation 91.12%）；编码与密钥扫描零问题；双跑字节一致。

**等待 Codex Gate 4A 最终复审，PR 不得合并。**
