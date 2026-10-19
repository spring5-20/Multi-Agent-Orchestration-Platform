"""编排器的确认机制：待确认请求的存储与 ``ConfirmationRequired`` 信号。"""

from dataclasses import dataclass
from typing import Any
from uuid import uuid4


class ConfirmationRequired(Exception):  # noqa: N818  这是「需要确认」的信号，不是错误
    """子智能体需要用户确认后才能执行的信号。"""

    def __init__(
        self,
        agent_name: str,
        message: str,
        context: dict[str, Any] | None,
        prompt: str,
    ) -> None:
        super().__init__(prompt)
        self.agent_name = agent_name
        self.message = message
        self.context = context
        self.prompt = prompt


@dataclass
class PendingConfirmation:
    """一条等待用户确认的请求。"""

    confirm_id: str
    agent_name: str
    message: str
    context: dict[str, Any] | None


# 进程内的待确认请求表；生产多实例部署时需换成 Redis / 数据库等共享存储。
_pending: dict[str, PendingConfirmation] = {}


def store_pending(agent_name: str, message: str, context: dict[str, Any] | None) -> str:
    """存入一条待确认请求，返回其 ``confirm_id``。"""
    confirm_id = uuid4().hex
    _pending[confirm_id] = PendingConfirmation(
        confirm_id=confirm_id,
        agent_name=agent_name,
        message=message,
        context=context,
    )
    return confirm_id


def pop_pending(confirm_id: str) -> PendingConfirmation | None:
    """取出并删除一条待确认请求；不存在返回 ``None``。"""
    return _pending.pop(confirm_id, None)
