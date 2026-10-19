"""A2A 客户端：用官方 a2a-sdk 把远程 A2A Agent 包装成编排器的子智能体。

生产化要点：
- 每个 ``A2ASubAgent`` 持有并复用一个 ``httpx.AsyncClient``（连接池 + 超时），
  多次 ``stream`` / ``confirm`` 之间复用连接，而非每次新建。
- 通过 ``ClientConfig(httpx_client=...)`` 把连接池注入官方 SDK。
- 对「连接建立 + 首个响应之前」的瞬态网络错误做指数退避重试。
- 实现「确认闭环」：远程返回 ``input-required`` 时保存待确认任务的
  taskId/contextId，通过 ``confirm()`` 回传用户确认恢复执行。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
from a2a import types
from a2a.client.client import ClientConfig
from a2a.client.client_factory import create_client

from ...config import settings
from ..orchestrator.protocol import BaseSubAgent

logger = logging.getLogger(__name__)

_RETRYABLE = (httpx.TransportError,)


def _message_text(message: types.Message) -> str:
    """提取消息里的全部文本 Part。"""
    return "".join(part.text for part in message.parts if part.WhichOneof("content") == "text")


def _artifact_text(artifact: types.Artifact) -> str:
    """提取 artifact 里的全部文本 Part。"""
    return "".join(part.text for part in artifact.parts if part.WhichOneof("content") == "text")


class A2ASubAgent(BaseSubAgent):
    """通过官方 A2A SDK 调用远程子智能体的客户端实现。

    与进程内适配器遵循同一 ``BaseSubAgent`` 契约，编排器代码无需改动。
    内部复用同一个 httpx 连接池，并对建立连接与首个响应前的瞬态错误做退避重试。
    """

    def __init__(
        self,
        name: str,
        description: str,
        url: str,
        *,
        confirmation_required: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.confirmation_required = confirmation_required
        self._url = url.rstrip("/")
        # 复用的连接池（惰性创建，aclose 释放）
        self._http_client: httpx.AsyncClient | None = None
        # 待确认任务的 taskId / contextId（确认闭环）
        self._pending_task_id: str | None = None
        self._pending_context_id: str | None = None

    @property
    def awaiting_confirmation(self) -> bool:
        """是否正等待用户确认。"""
        return self._pending_task_id is not None

    def _get_http_client(self) -> httpx.AsyncClient:
        """惰性创建并复用一个带连接池与超时的 httpx 客户端。"""
        client = self._http_client
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.A2A_CLIENT_TIMEOUT_SECONDS),
                limits=httpx.Limits(
                    max_connections=settings.A2A_CLIENT_MAX_CONNECTIONS,
                    max_keepalive_connections=settings.A2A_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
                ),
            )
            self._http_client = client
        return client

    def _build_request(self, message: str, *, context: dict[str, Any] | None = None) -> types.SendMessageRequest:
        """构造请求；处于待确认状态时回填 taskId/contextId，已确认时透传 confirmed。"""
        msg = types.Message(
            role=types.Role.ROLE_USER,
            message_id=uuid4().hex,
            parts=[types.Part(text=message)],
        )
        if self._pending_task_id:
            msg.task_id = self._pending_task_id
            if self._pending_context_id is not None:
                msg.context_id = self._pending_context_id
        request = types.SendMessageRequest(message=msg)
        if (context or {}).get("confirmed"):
            # 编排器已完成确认门禁，通知远端 A2A 服务端跳过自身的 input-required
            request.metadata["confirmed"] = "true"
        return request

    async def _iter_responses(self, request: types.SendMessageRequest) -> AsyncIterator[types.StreamResponse]:
        """带连接复用与退避重试地迭代远程响应。

        仅对「连接建立 + 首个响应之前」发生的瞬态网络错误重试；一旦已产出
        任意响应（``produced`` 为真），后续错误直接抛出，避免重复产出导致副作用。
        """
        max_retries = settings.A2A_CLIENT_MAX_RETRIES
        for attempt in range(max_retries + 1):
            produced = False
            try:
                client = await create_client(
                    self._url,
                    client_config=ClientConfig(httpx_client=self._get_http_client()),
                )
                async for response in client.send_message(request):
                    produced = True
                    yield response
                return
            except _RETRYABLE as exc:
                if produced or attempt >= max_retries:
                    raise
                delay = settings.A2A_CLIENT_RETRY_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "A2A 客户端调用 %s 失败（第 %d/%d 次），%.1f 秒后重试：%s",
                    self._url,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

    async def stream(self, message: str, *, context: dict[str, Any] | None = None) -> AsyncIterator[str]:
        """调用远程 A2A Agent，产出回答文本（流式 artifact 或确认提示）。

        若上次调用进入 ``input-required`` 且尚未确认，本次 ``message`` 会被当作
        确认回复回传（携带原 taskId/contextId），从而恢复执行——即确认闭环。
        """
        async for response in self._iter_responses(self._build_request(message, context=context)):
            payload = response.WhichOneof("payload")
            if payload == "task":
                task = response.task
                if task.status.state == types.TaskState.TASK_STATE_INPUT_REQUIRED:
                    self._pending_task_id = task.id
                    self._pending_context_id = task.context_id
                    if task.status.message is not None:
                        yield _message_text(task.status.message)
                else:
                    self._clear_pending()
                    for artifact in task.artifacts:
                        yield _artifact_text(artifact)
            elif payload == "message":
                self._clear_pending()
                yield _message_text(response.message)

    async def confirm(self, confirmation: str = "确认", *, context: dict[str, Any] | None = None) -> AsyncIterator[str]:
        """对上次返回 ``input-required`` 的任务回传确认，恢复执行并产出最终回答。"""
        if not self.awaiting_confirmation:
            raise RuntimeError("当前没有待确认任务，无法执行确认")
        async for chunk in self.stream(confirmation, context=context):
            yield chunk

    def _clear_pending(self) -> None:
        """清除待确认任务状态。"""
        self._pending_task_id = None
        self._pending_context_id = None

    async def aclose(self) -> None:
        """释放内部复用的 httpx 连接池。"""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
