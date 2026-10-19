"""智能问数的只读 SQL 执行器。

在独立连接上以「只读事务」执行已通过守卫的 SQL，并叠加两层超时保护：
数据库层的 statement_timeout 与 Python 层的 asyncio.wait_for。
结果会被转换为 JSON 安全的类型，供大模型解读与接口返回。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from ...config import settings
from ...db.database import async_engine


def json_safe(value: Any) -> Any:
    """把数据库返回值转换为 JSON 安全的类型。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def execute_readonly(sql: str, timeout_s: float | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """在只读事务中执行 SQL，返回 (columns, rows)。

    Args:
        sql: 已通过 guard_sql.guard 校验并夹逼 LIMIT 的只读 SQL。
        timeout_s: 执行超时（秒），缺省取 BI_QUERY_TIMEOUT_SECONDS。

    Returns:
        列名列表与行字典列表（值已转为 JSON 安全类型）。
    """
    timeout = settings.BI_QUERY_TIMEOUT_SECONDS if timeout_s is None else timeout_s
    statement_timeout_ms = max(int(timeout * 1000), 1)

    async def _run() -> tuple[list[str], list[dict[str, Any]]]:
        async with async_engine.connect() as conn, conn.begin():
            # 只读事务 + 语句超时：即使守卫被绕过，也不允许写操作与长查询
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text(f"SET LOCAL statement_timeout = {statement_timeout_ms}"))
            result = await conn.execute(text(sql))
            columns = list(result.keys())
            rows = [{column: json_safe(row._mapping[column]) for column in columns} for row in result.fetchall()]
        return columns, rows

    return await asyncio.wait_for(_run(), timeout=timeout + 1.0)
