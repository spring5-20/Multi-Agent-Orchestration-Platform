"""有界线程池：把阻塞的语音操作移出事件循环，并限制并发、提供超时。"""

import asyncio
import functools
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)


class WorkerPool:
    """围绕 :class:`ThreadPoolExecutor` 的异步封装。

    - ``max_workers``：底层线程数上限，阻塞任务在这里真正并发执行。
    - ``max_concurrency``：同时提交任务数的软上限，超过后调用方会在信号量上等待，
      避免高并发下向线程池灌入无界任务。
    - ``timeout_s``：单次任务超时；超时后底层线程任务仍会在后台跑完，
      但调用方会立刻收到超时异常，不再拖住 WebSocket 主流程。
    """

    def __init__(self, name: str, max_workers: int = 4, max_concurrency: int = 4) -> None:
        self._name = name
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def run(self, func: Callable[..., Any], *args: Any, timeout_s: float | None = None, **kwargs: Any) -> Any:
        """在线程池中执行阻塞函数，可加超时。"""
        loop = asyncio.get_running_loop()
        async with self._semaphore:
            call = functools.partial(func, *args, **kwargs)
            future = loop.run_in_executor(self._executor, call)
            if timeout_s is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout_s)
            except TimeoutError as exc:
                logger.warning("线程池任务超时: %s (%.1fs)", self._name, timeout_s)
                raise TimeoutError(f"{self._name} 操作超时（{timeout_s} 秒）") from exc

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """关闭线程池，停止接收新任务（幂等，可重复调用）。"""
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    async def close(self) -> None:
        """异步关闭线程池：等待运行中的任务完成并取消排队任务，不阻塞事件循环。"""
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
