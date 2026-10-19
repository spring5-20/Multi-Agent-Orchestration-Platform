"""智能问数图检查点工厂：用 PostgreSQL 持久化多轮对话状态。

与公告审批共用同一套「同步 PostgresSaver + 线程池包装成异步」的方案：
psycopg3 的 async 模式在 Windows 默认 ProactorEventLoop 下不可用，因此采用
同步 PostgresSaver + ConnectionPool，再把同步读写包成 async 方法。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio
from langgraph.checkpoint.base import BaseCheckpointSaver

from ...config import settings

logger = logging.getLogger(__name__)


class SyncToAsyncPostgresSaver(BaseCheckpointSaver):
    """把同步 PostgresSaver 包装为 LangGraph 异步检查点接口。"""

    def __init__(self, saver: Any) -> None:
        super().__init__()
        self._saver = saver

    async def aget_tuple(self, config: Any) -> Any:
        """异步读取检查点。"""
        return await anyio.to_thread.run_sync(self._saver.get_tuple, config)

    async def alist(
        self,
        config: Any,
        *,
        filter: dict[str, Any] | None = None,
        before: Any = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        """异步列出检查点历史。"""

        def _list() -> list[Any]:
            return list(self._saver.list(config, filter=filter, before=before, limit=limit))

        for item in await anyio.to_thread.run_sync(_list):
            yield item

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        """异步写入检查点。"""
        return await anyio.to_thread.run_sync(self._saver.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        """异步写入中间 writes。"""
        await anyio.to_thread.run_sync(self._saver.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """异步删除某线程的全部检查点。"""
        await anyio.to_thread.run_sync(self._saver.delete_thread, thread_id)


async def build_checkpointer() -> Any:
    """创建 PostgresSaver 检查点并初始化检查点表。

    检查点表（checkpoints / checkpoint_blobs / checkpoint_writes）由
    PostgresSaver.setup() 自动创建，无需 alembic 迁移。
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    sync_url = f"{settings.POSTGRES_SYNC_PREFIX}{settings.POSTGRES_URI}"
    pool = ConnectionPool(
        conninfo=sync_url,
        min_size=1,
        max_size=5,
        open=False,
        timeout=10,
        kwargs={"row_factory": dict_row},
    )
    pool.open(wait=True, timeout=10)
    saver = PostgresSaver(pool)  # type: ignore[arg-type]
    saver.setup()
    logger.info("智能问数检查点：PostgresSaver（已初始化 checkpoint 表）")
    return SyncToAsyncPostgresSaver(saver)
