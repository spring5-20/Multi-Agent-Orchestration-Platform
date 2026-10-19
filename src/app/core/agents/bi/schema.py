"""智能问数的表结构元数据：运行时内省数据库 catalog 并缓存。

「事实来源」是 PostgreSQL 的 catalog（information_schema / pg_catalog），
而不是 Base.metadata 里登记的 ORM 模型。因此无论表是通过 Alembic 迁移创建、
还是 DBA 手工创建，只要真实存在于数据库中，就能被内省到并自动进入问数范围。

内省结果会缓存一段时间（BI_SCHEMA_CACHE_TTL_SECONDS），避免每次提问都查询
catalog；期间新建的表会延迟可见，可通过 invalidate 手动刷新。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import inspect

from ...config import settings
from ...db.database import async_engine

# 表结构缓存：{表名: {"comment": str | None, "columns": [{"name", "type", "comment"}]}}
_cache: dict[str, dict[str, Any]] | None = None
_cache_ts: float = 0.0
_lock = asyncio.Lock()


def table_blocklist() -> set[str]:
    """把配置中的禁止问数表白名单拆成小写表名集合。"""
    return {name.strip().lower() for name in settings.BI_TABLE_BLOCKLIST.split(",") if name.strip()}


def sensitive_columns() -> set[str]:
    """把配置中的敏感列拆成小写列名集合。"""
    return {name.strip().lower() for name in settings.BI_SENSITIVE_COLUMNS.split(",") if name.strip()}


def _introspect_sync(sync_conn: Any) -> dict[str, dict[str, Any]]:
    """在同步连接上执行 catalog 内省，返回表结构字典。"""
    inspector = inspect(sync_conn)
    blocked = table_blocklist()
    sensitive = sensitive_columns()

    schema: dict[str, dict[str, Any]] = {}
    for table_name in inspector.get_table_names():
        if table_name.lower() in blocked:
            continue
        try:
            table_comment = inspector.get_table_comment(table_name)
            raw_columns = inspector.get_columns(table_name)
        except Exception:  # noqa: BLE001 - 个别表元数据不可读时跳过，不阻塞整体内省
            continue
        columns: list[dict[str, Any]] = []
        for column in raw_columns:
            name = str(column.get("name", ""))
            # 敏感列不进入注入大模型的 schema，避免被生成 SQL 引用
            if name.lower() in sensitive:
                continue
            columns.append(
                {
                    "name": name,
                    "type": str(column.get("type", "")),
                    "comment": column.get("comment"),
                }
            )
        schema[table_name] = {
            "comment": table_comment.get("text") if isinstance(table_comment, dict) else None,
            "columns": columns,
        }
    return schema


async def _introspect() -> dict[str, dict[str, Any]]:
    """在独立连接上执行内省（run_sync 把同步 Inspector 调用放到线程池）。"""
    async with async_engine.connect() as conn:
        return await conn.run_sync(_introspect_sync)


async def get_schema(force: bool = False) -> dict[str, dict[str, Any]]:
    """获取表结构字典，优先命中缓存；force=True 时强制刷新。"""
    global _cache, _cache_ts
    now = time.monotonic()
    ttl = settings.BI_SCHEMA_CACHE_TTL_SECONDS
    if _cache is not None and not force and (ttl <= 0 or now - _cache_ts < ttl):
        return _cache

    async with _lock:
        now = time.monotonic()
        if _cache is not None and not force and (ttl <= 0 or now - _cache_ts < ttl):
            return _cache
        _cache = await _introspect()
        _cache_ts = time.monotonic()
        return _cache


async def invalidate() -> None:
    """立即失效表结构缓存（供刷新接口调用）。"""
    global _cache, _cache_ts
    async with _lock:
        _cache = None
        _cache_ts = 0.0


def render_schema(schema: dict[str, dict[str, Any]]) -> str:
    """把表结构字典渲染成注入大模型的紧凑文本。"""
    lines: list[str] = []
    for table_name, meta in schema.items():
        comment = meta.get("comment")
        header = f"表 {table_name}"
        if comment:
            header += f"（{comment}）"
        lines.append(header + "：")
        for column in meta.get("columns", []):
            col_type = column.get("type") or ""
            col_comment = column.get("comment")
            entry = f"  - {column['name']}: {col_type}"
            if col_comment:
                entry += f"（{col_comment}）"
            lines.append(entry)
    return "\n".join(lines)
