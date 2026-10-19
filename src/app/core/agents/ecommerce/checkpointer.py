"""短期记忆 Checkpointer 工厂：按配置创建并处理后端回退。

与长期记忆（`memory`）、商品源（`product`）、窗口策略（`window`）
并列的第四类运行期依赖；单例由 `runtime` 统一管理。
"""

import contextlib
import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from ...config import settings

logger = logging.getLogger(__name__)


async def build_checkpointer() -> Any:
    """按配置创建短期记忆 Checkpointer，Postgres 后端不可用时优雅回退。

    ``postgres`` 后端使用 LangGraph 官方的 `AsyncPostgresSaver`：
    它自己管理连接池与建表，因此这里创建一个常驻的 `psycopg` 异步连接池交给它，
    并在应用退出时由 :func:`runtime.close_checkpointer` 释放。

    连接失败（未部署数据库、网络不通等）时回退到进程内 `MemorySaver`，
    保证开发环境下对话链路仍然可用，不再因缺少依赖或连不上库而启动失败。
    """
    backend = settings.ECOMMERCE_CHECKPOINT_BACKEND
    if backend != "postgres":
        logger.info("短期记忆 Checkpointer：MemorySaver（进程内）")
        return MemorySaver()

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        logger.warning(
            "未安装 langgraph-checkpoint-postgres 或 psycopg-pool（%s），短期记忆回退进程内 MemorySaver；"
            "如需持久化短期记忆，请执行 uv add langgraph-checkpoint-postgres",
            exc,
        )
        return MemorySaver()

    sync_url = f"{settings.POSTGRES_SYNC_PREFIX}{settings.POSTGRES_URI}"
    pool: Any = None
    try:
        pool = AsyncConnectionPool(
            conninfo=sync_url,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        # wait=True 让「连不上库」在这里立刻暴露并回退，而不是等到首次对话才报错
        await pool.open(wait=True, timeout=5.0)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        logger.info("短期记忆 Checkpointer：AsyncPostgresSaver（持久化）")
        return saver
    except Exception as exc:  # noqa: BLE001
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        logger.warning("Postgres Checkpointer 初始化失败，短期记忆回退进程内 MemorySaver: %s", exc)
        return MemorySaver()
