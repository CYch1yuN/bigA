# A 股双轨量化研究系统

本仓库用于完全免费的 A 股个股量化研究。系统不连接券商、不执行真实交易，也不承诺收益。

## 协作方式

- Trae + GLM-5.2：按 `docs/trae/` 中的阶段任务实现代码与测试。
- Codex：按 `docs/review-gates.md` 审核数据、回测、策略与自动化。
- 每个阶段使用独立的 `trae/phase-*` 分支和 Pull Request；上一阶段通过后再开始下一阶段。

当前阶段：`phase-1-data`（数据层）。

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

pip install -e ".[dev]"
```

如需实际抓取行情（非离线测试），额外安装数据源 SDK：

```bash
pip install -e ".[sources]"
```

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

## 相关文档

- 阶段任务：`docs/trae/phase-1-data.md`
- 审核闸门：`docs/review-gates.md`
- 已知缺口：`docs/phase-1-limitations.md`
- 示例报告：`reports/phase-1/`
