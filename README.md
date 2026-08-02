# A 股双轨量化研究系统

本仓库用于完全免费的 A 股个股量化研究。系统不连接券商、不执行真实交易，也不承诺收益。

## 协作方式

- Trae + GLM-5.2：按 `docs/trae/` 中的阶段任务实现代码与测试。
- Codex：按 `docs/review-gates.md` 审核数据、回测、策略与自动化。
- 每个阶段使用独立的 `trae/phase-*` 分支和 Pull Request；上一阶段通过后再开始下一阶段。

当前阶段：`phase-2-backtester`（任务规格已就绪，等待Trae实现）。

## 安全要求

- 禁止提交 API Key、Cookie、券商账户或其他凭据。
- 完整行情数据不进入 Git，只提交数据清单、哈希、最小测试样本和报告。
- 研究结论不得表述为收益承诺。

## 项目结构

```
src/ashare_quant/        源码（src 布局）
  config.py              YAML 配置 + Pydantic 模型
  constants.py           schema 版本与规范字段
  schema.py              Pydantic 校验模型 + Arrow schema
  standardize.py         原始 -> curated 标准化 + 内容哈希
  storage.py             Parquet 存储 + DuckDB 查询 + SHA-256
  quality.py             10 类数据质量检查
  manifest.py            数据版本清单
  reports.py             报告生成
  samples.py             合成样本构建器（测试与示例用）
  cli.py                 命令行接口
  providers/             数据源适配器（AKShare / BaoStock）
config/default.yaml      质量阈值与配置（禁止硬编码阈值）
tests/                   离线测试与合成样本
reports/phase-1/         Phase 1 示例报告
docs/                    阶段任务、审核闸门与限制说明
```

## 安装

要求 Python 3.11+。

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -e ".[workbench]"
```

`.[workbench]` 是**可操作工作台（Dashboard + 真实数据源）的唯一推荐安装入口**，
一次安装即同时具备：

- 核心量化库（duckdb / pyarrow / pandas / pydantic）
- Dashboard 后端（FastAPI / uvicorn / argon2 / httpx）
- 真实行情数据源（AKShare 主 + BaoStock 备）
- 测试工具链（pytest / pytest-cov）

> 只需离线测试/标准化（不需要 Dashboard 与抓取）时可用 `pip install -e ".[dev]"`；
> 只需要抓取不需要工作台时可用 `pip install -e ".[sources]"`。

## 启动可操作工作台

### 0. 构建前端（干净克隆必须执行；dist 不进入提交）

```bash
cd dashboard/frontend
npm ci
npm run build
cd ../..
```

> `npm ci` 按 `package-lock.json` 精确安装依赖；`npm run build` 产出 `dashboard/frontend/dist`。
> 若跳过此步直接启动后端，启动脚本将明确提示"前端未构建"。

### 1. 准备凭据（仓库根 `.env`，Git 忽略）

在仓库根创建 `.env`，写入三项（**明文密码请存入密码管理器，不要写入 `.env`**）：

```
ASHARE_DASHBOARD_USERNAME=admin
ASHARE_DASHBOARD_PASSWORD_HASH=<argon2 哈希>
ASHARE_DASHBOARD_SESSION_SECRET=<至少 32 字符随机串>
```

哈希生成示例（用你的密码替换 `你的密码`）：

```powershell
.\.venv\Scripts\python.exe -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('你的密码'))"
```

### 2. 启动服务（默认 http://127.0.0.1:8765）

**Windows（PowerShell，推荐）：**

```powershell
.\start_dashboard.ps1            # 检查 .env / venv / 前端 dist 后启动
.\start_dashboard.ps1 -CheckOnly # 仅预检，不启动
```

**Linux / macOS：**

```bash
# .env 中哈希含 $ 字符，请逐项手动 export（哈希/secret 用单引号包裹）
export ASHARE_DASHBOARD_USERNAME="admin"
export ASHARE_DASHBOARD_PASSWORD_HASH='<从 .env 复制>'
export ASHARE_DASHBOARD_SESSION_SECRET='<从 .env 复制>'
.venv/bin/python -m uvicorn app.main:create_app --factory --app-dir dashboard/backend --host 127.0.0.1 --port 8765
```

> 首次启动会把密码哈希持久化到 `state/dashboard/auth.json`（Git 忽略），此后其优先级高于 `.env`；
> 后续轮换密码请在登录后通过界面「修改密码」完成（自动更新 auth.json 并使旧会话失效）。

启动后打开 http://127.0.0.1:8765 登录，首页「操作中心」可执行环境检查、
每日/每周任务、日期区间补跑与失败重跑——均通过本地自动化 CLI 真实执行，
仅作用于模拟账户，不连接券商、不涉及真实资金。

> 验证环境就绪：`ashare-quant automation verify`（会检查 AKShare/BaoStock 是否可导入）。

## 测试

所有单元测试离线运行，不访问 AKShare/BaoStock 公网接口。

```bash
pytest
```

带覆盖率：

```bash
pytest --cov=ashare_quant --cov-report=term-missing
```

## CLI 使用

```bash
# 查看或初始化配置
ashare-quant init-config
ashare-quant init-config --output my-config.yaml

# 抓取指定股票和日期范围（需安装 [sources]）
ashare-quant fetch --symbol 000001 --start 2024-01-02 --end 2024-03-15 --source akshare

# 标准化本地原始数据
ashare-quant standardize --input data/raw/ak.parquet --output data/curated/c.parquet --source akshare

# 执行质量检查（严重问题返回非零退出码）
ashare-quant quality --input data/curated/c.parquet --calendar data/metadata/cal.parquet
ashare-quant quality --input c.parquet --cross-source other.parquet --reports-dir reports/phase-1

# 生成数据版本清单
ashare-quant manifest --input c.parquet --source akshare --symbol 000001 \
  --start 2024-01-02 --end 2024-03-15 --files curated=c.parquet --output manifest.json

# 运行完整 Phase 1 离线示例（生成全部报告）
ashare-quant run-example --reports-dir reports/phase-1
```

## 数据分层

- `data/raw`：数据源原生原始数据。
- `data/curated`：标准化后的规范 schema 数据。
- `data/metadata`：交易日历、证券主数据等元数据。

Parquet 用于文件存储，DuckDB 用于查询。`data/` 目录与完整行情数据不进入 Git。

## 关键设计

- **复权信号列与未复权成交列分离**：`*_qfq`、`adjustment_factor` 用于信号生成；`*_raw`、`volume`、`amount` 用于成交模拟。
- **避免幸存者偏差**：证券主数据保留上市/退市/ST 状态区间历史，不使用当前股票列表替代历史股票池。已知缺口见 `docs/phase-1-limitations.md`。
- **质量检查**：10 类检查，严重问题阻止下游并返回非零退出码，同时生成 JSON 与 Markdown 报告。阈值集中在 `config/default.yaml`。
- **可复现性**：`fetched_at` 等非确定字段在内容哈希计算时排除，相同输入与配置产生稳定哈希。
- **网络解耦**：数据源 SDK 惰性导入，网络获取集中在 `_call_*` 方法，标准化与质量检查独立于网络。

## 可操作工作台：验收状态（如实说明）

工作台（Dashboard 操作中心）通过固定 argv 白名单调用真实 `automation` CLI，
执行环境检查、每日/每周任务、区间补跑与失败重跑；作业状态持久化在
`state/dashboard/jobs/`，仅作用于模拟账户。

**当前验证结论（2026-08-02）**：

- 已接通并验证：真实数据源调用链（AKShare 主 + BaoStock 备）、交易日历筛选、
  逐日串行补跑、状态机（queued/running/succeeded/partial/failed/skipped/interrupted）、
  连续异常升级、令牌参数绑定、严格旁路（校验失败不阻断主流程）。
- **尚未在可访问行情接口的网络中验证**：真实抓取并生成完整行情/信号/模拟账户产物。
  在开发沙箱中，东方财富行情接口因网络出口限制无法访问，daily 如实降级为
  `skipped`（`SKIPPED_DATA_UNAVAILABLE`），**未虚构成功**。
- **待外部环境验证项**：在可访问 AKShare/BaoStock 行情接口的网络中运行
  `ashare-quant automation daily --date <交易日>`，确认生成 curated 行情、
  研究信号与模拟账户产物后，方可宣称"真实数据产物端到端成功"。

运行 `ashare-quant automation verify` 可检查数据源 SDK 是否可导入。

## 相关文档

- 阶段任务：`docs/trae/phase-1-data.md`
- Phase 2任务：`docs/trae/phase-2-backtester.md`
- 审核闸门：`docs/review-gates.md`
- 已知缺口：`docs/phase-1-limitations.md`
- 示例报告：`reports/phase-1/`
