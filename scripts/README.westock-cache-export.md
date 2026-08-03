# westock_cache_export：Westock 缓存导出（Phase A）

把 WorkBuddy 会话内获取的 Westock 响应**标准化后**写入 Dashboard 只读缓存。

## 为什么存在

Dashboard 独立进程当前未配置可用的 MCP 直连授权（连接器授权上下文只在
WorkBuddy 会话内；历史无凭据探测未获得可用授权）。因此 Dashboard 通过
`transport=cache_export` 消费缓存，**不声明 MCP 直连**、**不把缓存标为实时**。
探测结果属于"当前运行模式"，不硬编码为永久事实。

## 用法

```powershell
# 在 WorkBuddy 会话内取得某个能力（如 data_quote）的响应，保存为 JSON：
#   响应结构：{"ok": true, "data": {...}}（取 data 写入缓存）

.\.venv\Scripts\python.exe scripts\westock_cache_export.py `
  --capability quote `
  --input C:\tmp\quote_sh600519.json `
  --scope sh600519 `
  --as-of 2026-08-03 `
  --fetched-at 2026-08-03T12:00:00+00:00
```

单行形式（PowerShell）：

```powershell
.\.venv\Scripts\python.exe scripts\westock_cache_export.py --capability quote --input C:\tmp\quote_sh600519.json --scope sh600519
```

- `--capability`：能力名（`westock_bridge.CAPABILITY_MAP` 白名单，只读）
- `--input`：响应 JSON；**超过 5 MiB 拒绝**；`ok=false` 响应拒绝写入
- `--scope`：缓存键（默认 `global`；仅字母数字 `._-`，禁止路径分隔符）
- `--as-of` / `--fetched-at`：可选；`fetched_at` 用于 fresh/stale 判定

写入采用**原子替换**（临时文件 + fsync + `os.replace`），失败不会破坏旧缓存。

## 边界

- 只接受 WorkBuddy 已验证的只读能力；不写入模拟交易、自选股写、提醒写。
- 本脚本不调用 MCP、不接触授权凭据；所有认证发生在 WorkBuddy 会话内。
- 缓存位于 `state/dashboard/westock/`（Git 忽略），不进任何提交。
- Westock `qfq/hfq` 曾与 `raw` 返回一致，禁止作为复权/回测数据源。
