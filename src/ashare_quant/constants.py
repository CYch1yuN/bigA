"""模块级常量：schema 版本、规范字段定义、字段分组。

关键设计：复权（前复权）价格列用于信号生成，未复权价格与成交列用于成交模拟，
两组在 schema 中明确分离，避免回测时混用。
"""
from __future__ import annotations

# ---- Schema 版本 ----
DAILY_QUOTE_SCHEMA_VERSION = "1.0.0"
SECURITY_MASTER_SCHEMA_VERSION = "1.0.0"

# ---- 日行情规范字段 ----
# 顺序即 Parquet/DuckDB 列顺序。
DAILY_QUOTE_FIELDS: list[str] = [
    "symbol",            # 股票代码，如 000001
    "trade_date",        # 交易日 YYYY-MM-DD
    "open_raw",          # 未复权开盘价
    "high_raw",          # 未复权最高价
    "low_raw",           # 未复权最低价
    "close_raw",         # 未复权收盘价
    "volume",            # 成交量（股）
    "amount",            # 成交额（元）
    "open_qfq",          # 前复权开盘价（信号用）
    "high_qfq",          # 前复权最高价（信号用）
    "low_qfq",           # 前复权最低价（信号用）
    "close_qfq",         # 前复权收盘价（信号用）
    "adjustment_factor", # 复权因子
    "is_suspended",      # 是否停牌
    "is_tradable",       # 是否可交易
    "source",            # 数据源 akshare/baostock
    "fetched_at",        # 抓取时间（非确定字段）
]

# 日行情主键
DAILY_QUOTE_PRIMARY_KEY: list[str] = ["symbol", "trade_date"]

# 未复权成交列（用于成交模拟）
TRADEABLE_PRICE_FIELDS: list[str] = ["open_raw", "high_raw", "low_raw", "close_raw"]
TRADEABLE_VOLUME_FIELDS: list[str] = ["volume", "amount"]

# 前复权信号列（用于信号生成）
SIGNAL_PRICE_FIELDS: list[str] = ["open_qfq", "high_qfq", "low_qfq", "close_qfq"]

# 非确定字段（内容哈希与复现性测试排除）
NONDETERMINISTIC_FIELDS: list[str] = ["fetched_at", "observed_at"]

# ---- 证券主数据字段 ----
SECURITY_MASTER_FIELDS: list[str] = [
    "symbol",            # 股票代码
    "name",              # 证券名称
    "list_date",         # 上市日
    "delist_date",       # 退市日（未退市为空）
    "board",             # 板块 main/star/szse/bjse
    "st_status",         # ST 状态 normal/st/*st/pt/unknown
    "status_valid_from", # 状态生效起始日（未知则为空，不伪造）
    "status_valid_to",   # 状态生效结束日（空表示至今）
    "observed_at",       # 快照观察时间（抓取时间，非状态生效日）
]

SECURITY_MASTER_PRIMARY_KEY: list[str] = ["symbol", "status_valid_from"]

# ---- 数据源标识 ----
SOURCE_AKSHARE = "akshare"
SOURCE_BAOSTOCK = "baostock"

# ---- 数据分层目录名 ----
LAYER_RAW = "raw"
LAYER_CURATED = "curated"
LAYER_METADATA = "metadata"
