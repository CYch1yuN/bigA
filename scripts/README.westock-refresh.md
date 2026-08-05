# Westock 刷新请求工作流（Phase F3）

> Dashboard 不直连 MCP；WorkBuddy 会话持有 connector 授权。
> 本脚本只推进请求状态与写入缓存，**绝不调用 MCP，也不宣称自动刷新**。

## 角色

- **Dashboard 后端**：创建 / 查询 / 取消刷新请求（`POST /api/connections/westock/refresh` 或
  `/refresh-requests`）。`connection_status.connected` 恒为 `false`（cache_export 诚实语义）。
- **WorkBuddy 会话**（你，本助手）：读取请求 → 调用 westock MCP → 原始响应存**仓库外**
  临时目录 → `export` 写入缓存 → 完成。
- **worker CLI**（本目录 `westock_refresh_request.py`）：排队状态机 + 受控导出。

## 真实操作流程

```text
1. list                      查看待处理请求（pending）
2. claim <request_id>        认领（pending→processing），获得 scope/capabilities
3. （WorkBuddy 会话内）调用 westock MCP 获取该能力数据
4. 原始响应保存到仓库外，例如 C:\Users\Administrator\AppData\Local\Temp\BigA-F3-raw\
5. export <request_id> --input <原始响应.json> --capability <name>
                             写入 state/dashboard/westock/<cap>/<scope>.json（原子）
6. complete-job <request_id> --capability <name> --result ok|partial|failed
                             记录单能力结果（幂等；冲突报错）
7. finish <request_id>       聚合 → completed / partial / failed，写回执
```

## 状态机

```text
pending ──claim──▶ processing ──finish──▶ completed | partial | failed
   │                  │
   ├─24h 过期──▶ expired      ├─2h worker_timeout──▶ expired
   └─DELETE──▶ cancelled
```

- `pending` 24 小时未认领 → `expired`
- `processing` 认领后 2 小时未 finish → `expired`（worker_timeout）
- `DELETE /refresh-requests/{id}` 仅可取消 `pending`

## 安全约束

- 请求文件 ≤64 KiB；`request_id` 严格 32 位小写 hex
- 请求体递归禁止 `token/password/secret/cookie/authorization/credential` 字段
- 相同请求（canonical hash）在 pending/processing 中自动去重
- session 仅存不可逆指纹（sha256），原始 session ID 不落盘
- worker_id 仅不可逆指纹（host+pid+随机 sha256）
- `export` 输入 ≤5 MiB；`ok=false` / 非法 schema / capability 不在请求范围 / 非法 scope 一律拒绝；
  原子写入，失败不破坏旧缓存
- 回执（receipt）仅白名单摘要：request_id / 状态 / 时间 / 各能力 result——不含
  data、原始响应、路径、tool、token、堆栈
- 30 天受控清理：`RefreshStore.prune()`（不安装计划任务）

## target / preset 白名单

| target | preset | 能力 |
|---|---|---|
| stock | basic | quote, profile, news, fund_flow |
| stock | fundamentals | financials, forecast |
| stock | ownership | shareholders, dividend, buyback |
| stock | funds | margin, block_trade, northbound, chip_distribution |
| stock | intel | reports, announcements |
| stock | events_risk | events, risk |
| stock | technical | technical, minute |
| market | overview | market_overview, change_distribution, hot_ranking |
| market | reference | sector, index, industry_chain, macro |
| screener | filter | filter |
| screener | strategy | strategy_select |
| screener | ranking | factor_ranking |
| screener | label | label_select |

- `stock` 必须提供合法 symbol（如 `600519.SH`），scope = symbol
- `market` / `screener` 不需要 symbol，scope = global

## CLI 子命令

```text
python scripts/westock_refresh_request.py list [--status pending|...]
python scripts/westock_refresh_request.py claim <request_id>
python scripts/westock_refresh_request.py export <request_id> --input <file> --capability <name>
python scripts/westock_refresh_request.py complete-job <request_id> --capability <name> --result ok|partial|failed [--warning ...]
python scripts/westock_refresh_request.py finish <request_id>
```

## 目录

- 请求/回执/索引：`state/dashboard/westock-refresh/`（ignored，不入 Git）
  - `requests/<request_id>.json`、`receipts/<request_id>.json`、`index.json`
- 缓存：`state/dashboard/westock/<capability>/<scope>.json`（ignored）
- 原始 MCP 响应：**仓库外**临时目录（如 `%LOCALAPPDATA%\Temp\BigA-F3-raw\`）
