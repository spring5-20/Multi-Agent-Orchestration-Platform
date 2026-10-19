"""智能问数的运行期单例：检查点与编译后的图。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import anyio

from .checkpointer import build_checkpointer

logger = logging.getLogger(__name__)

_checkpointer: Any | None = None
_graph: Any | None = None
_lock = asyncio.Lock()


async def get_checkpointer() -> Any:
    """获取问数图使用的检查点单例（PostgreSQL 持久化，用于多轮指代）。"""
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = await build_checkpointer()
    return _checkpointer


async def get_graph() -> Any:
    """获取编译好的问数图单例；检查点初始化失败时降级为无状态图，保证接口可用。"""
    global _graph
    if _graph is not None:
        return _graph
    async with _lock:
        if _graph is not None:
            return _graph
        from .graph import build_bi_graph

        try:
            checkpointer = await get_checkpointer()
        except Exception as exc:  # noqa: BLE001 - 无数据库时仍允许问数（单轮无记忆）
            logger.warning("智能问数检查点初始化失败，降级为无状态模式: %s", exc)
            checkpointer = None
        _graph = build_bi_graph(checkpointer=checkpointer)
        return _graph


def reset_graph() -> None:
    """清空编译后的图与检查点单例（测试用）。"""
    global _graph, _checkpointer
    _graph = None
    _checkpointer = None


async def close_checkpointer() -> None:
    """关闭检查点连接池（应用退出时调用，避免进程退出时线程池告警）。"""
    global _graph, _checkpointer
    saver = _checkpointer
    _graph = None
    _checkpointer = None
    if saver is None:
        return
    inner = getattr(saver, "_saver", None)
    pool = getattr(inner, "conn", None)
    close = getattr(pool, "close", None)
    if close is None:
        return
    try:
        await anyio.to_thread.run_sync(close)
    except Exception as exc:  # noqa: BLE001 - 退出清理失败只告警
        logger.warning("智能问数检查点连接池关闭失败: %s", exc)
