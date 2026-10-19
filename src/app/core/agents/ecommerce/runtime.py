"""电商导购智能体的运行期单例：记忆存储与商品源。

独立成模块以避免 ``graph`` 与 ``tools`` 之间的循环导入，
两者都从这里按配置获取单例。
"""

import inspect
import logging
from typing import Any

from .checkpointer import build_checkpointer
from .memory.base import MemoryStore, build_memory_store
from .product.base import ProductSource, build_product_source
from .window import WindowStrategy, build_window_strategy

logger = logging.getLogger(__name__)

_source: ProductSource | None = None
_window: WindowStrategy | None = None
_store: MemoryStore | None = None
_checkpointer: Any = None


def get_product_source() -> ProductSource:
    """获取商品检索源单例（根据配置切换后端）。"""
    global _source
    if _source is None:
        _source = build_product_source()
    return _source


def get_window_strategy() -> WindowStrategy:
    """获取短期记忆窗口管理策略单例（根据配置切换策略）。"""
    global _window
    if _window is None:
        _window = build_window_strategy()
    return _window


def get_memory_store() -> MemoryStore:
    """获取长期记忆存储单例（根据配置切换后端）。"""
    global _store
    if _store is None:
        _store = build_memory_store()
    return _store


async def get_checkpointer() -> Any:
    """获取短期记忆 Checkpointer 单例（根据配置切换后端，缺失时回退）。"""
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = await build_checkpointer()
    return _checkpointer


async def close_checkpointer() -> None:
    """释放短期记忆 Checkpointer 常驻的资源（幂等，供应用退出时调用）。"""
    global _checkpointer
    saver, _checkpointer = _checkpointer, None
    if saver is None:
        return
    # Postgres Checkpointer 内部持有连接池（conn），需要显式关闭；MemorySaver 没有该属性
    close = getattr(getattr(saver, "conn", None), "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("释放短期记忆 Checkpointer 失败: %s", exc)
