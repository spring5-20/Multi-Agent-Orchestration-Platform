"""公告审批智能体的运行期单例：检查点。"""

from typing import Any

from .checkpointer import build_checkpointer

_checkpointer: Any | None = None


async def get_checkpointer() -> Any:
    """获取公告审批图使用的检查点单例（PostgreSQL 持久化）。"""
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = await build_checkpointer()
    return _checkpointer