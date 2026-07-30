"""存储模块：Parquet 文件存储 + DuckDB 查询 + 文件哈希。

数据分层目录：raw / curated / metadata，默认位于 git 忽略的 data/ 下。
Parquet 用于持久化，DuckDB 用于查询与分析。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .constants import LAYER_CURATED, LAYER_METADATA, LAYER_RAW
from .schema import daily_quote_arrow_schema, security_master_arrow_schema


def file_sha256(path: str | Path) -> str:
    """计算文件 SHA-256（分块读取）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Storage:
    """Parquet + DuckDB 存储管理。"""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / LAYER_RAW
        self.curated_dir = self.data_dir / LAYER_CURATED
        self.metadata_dir = self.data_dir / LAYER_METADATA
        for d in (self.raw_dir, self.curated_dir, self.metadata_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- Parquet ----
    def write_daily_quotes_parquet(
        self, df: pd.DataFrame, filename: str, layer: str = LAYER_CURATED
    ) -> Path:
        """写日行情 Parquet，按 schema 强制列类型。"""
        target_dir = self._layer_dir(layer)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / filename
        table = pa.Table.from_pandas(df, schema=daily_quote_arrow_schema(), preserve_index=False)
        pq.write_table(table, path, compression="snappy")
        return path

    def write_security_master_parquet(
        self, df: pd.DataFrame, filename: str, layer: str = LAYER_CURATED
    ) -> Path:
        target_dir = self._layer_dir(layer)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / filename
        table = pa.Table.from_pandas(
            df, schema=security_master_arrow_schema(), preserve_index=False
        )
        pq.write_table(table, path, compression="snappy")
        return path

    def write_generic_parquet(
        self, df: pd.DataFrame, filename: str, layer: str = LAYER_RAW
    ) -> Path:
        """写通用 Parquet（原始层，保留数据源原生字段）。"""
        target_dir = self._layer_dir(layer)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / filename
        df.to_parquet(path, index=False, compression="snappy")
        return path

    def read_parquet(self, path: str | Path) -> pd.DataFrame:
        return pd.read_parquet(path)

    def read_parquet_dir(self, layer: str, pattern: str = "*.parquet") -> pd.DataFrame:
        """读取某层目录下所有匹配 Parquet 并拼接。"""
        d = self._layer_dir(layer)
        files = sorted(d.glob(pattern))
        if not files:
            return pd.DataFrame()
        return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

    # ---- DuckDB ----
    def query(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        """在 DuckDB 内存中执行查询并返回 DataFrame。

        对于 Phase 1，DuckDB 直接查询 Parquet 文件或内存表。
        """
        con = duckdb.connect(database=":memory:")
        try:
            return con.execute(sql, params or []).fetchdf()
        finally:
            con.close()

    def query_parquet(self, parquet_path: str | Path, sql: str) -> pd.DataFrame:
        """对单个 Parquet 文件执行 DuckDB 查询。"""
        con = duckdb.connect(database=":memory:")
        try:
            rel = con.read_parquet(str(parquet_path))
            con.register("t", rel)
            return con.execute(sql).fetchdf()
        finally:
            con.close()

    # ---- 目录 ----
    def _layer_dir(self, layer: str) -> Path:
        return {
            LAYER_RAW: self.raw_dir,
            LAYER_CURATED: self.curated_dir,
            LAYER_METADATA: self.metadata_dir,
        }[layer]


__all__ = ["Storage", "file_sha256"]
