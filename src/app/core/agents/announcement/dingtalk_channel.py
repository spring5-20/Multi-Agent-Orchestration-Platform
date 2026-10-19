"""公告发布的钉钉通道：群名解析、群公告发布与审批通知。

复用钉协助手智能体的 dws CLI 安全封装，在异步上下文中通过线程池执行同步子进程，
避免阻塞事件循环。发布通道直接对接钉钉，不提供 mock 实现。
"""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import anyio

from ...config import settings
from ..dingtalk import cli as dingtalk_cli

logger = logging.getLogger(__name__)

_CID_PATTERN = re.compile(r"cid[A-Za-z0-9+/=]{10,}")


async def _run_dws(action: str, **params: Any) -> str:
    """在线程池中执行同步 dws 子命令，避免阻塞事件循环。"""

    def _call() -> str:
        return dingtalk_cli.run_dws(action, **params)

    return await anyio.to_thread.run_sync(_call)


def _extract_conversation_id(output: str) -> str:
    """从群搜索结果中解析 openConversationId。

    兼容 JSON 输出与纯文本输出：优先按字段名提取，兜底用 ``cid`` 前缀正则匹配。
    """
    keys = ("openConversationId", "conversationId", "conversation_id", "cid")
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
            for item in data.get("items") or data.get("groups") or data.get("results") or []:
                if isinstance(item, dict):
                    for key in keys:
                        value = item.get(key)
                        if isinstance(value, str) and value:
                            return value
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    for key in keys:
                        value = item.get(key)
                        if isinstance(value, str) and value:
                            return value
    except (json.JSONDecodeError, TypeError):
        pass

    match = _CID_PATTERN.search(output)
    if match:
        return match.group(0)
    raise RuntimeError(f"无法从群搜索结果解析 openConversationId，原始输出：{output[:400]}")


async def _resolve_conversation_id() -> str:
    """解析目标群的 openConversationId，优先使用显式配置。"""
    if settings.ANNOUNCEMENT_NOTICE_CONVERSATION_ID:
        return settings.ANNOUNCEMENT_NOTICE_CONVERSATION_ID
    group = settings.ANNOUNCEMENT_NOTICE_GROUP
    output = await _run_dws("group_search", query=group)
    conversation_id = _extract_conversation_id(output)
    logger.info("公告目标群「%s」解析出 openConversationId=%s", group, conversation_id)
    return conversation_id


async def publish_notice_to_group(title: str, content: str) -> str:
    """把公告发布到目标钉钉群（群公告，Markdown 正文）。"""
    conversation_id = await _resolve_conversation_id()
    body = f"**{title}**\n\n{content}"
    return await _run_dws("notice_create", conversation_id=conversation_id, content=body, confirm=True)


async def notify_pending_approval(state: Mapping[str, Any]) -> str:
    """向审批人推送待审批通知（群机器人 Webhook）。"""
    announcement_id = state.get("announcement_id", "")
    title = state.get("title", "")
    content = state.get("content", "")
    keyword = settings.ANNOUNCEMENT_NOTIFY_KEYWORD
    keyword_prefix = f"{keyword} " if keyword else ""
    text = (
        f"{keyword_prefix}【审批通知】待审批公告 #{announcement_id}\n"
        f"标题：{title}\n"
        f"内容：{content[:200]}\n"
        f"请回填审批结果：approve / edit / reject / cancel"
    )
    token = settings.DINGTALK_ROBOT_WEBHOOK_TOKEN
    if not token:
        raise RuntimeError("未配置 DINGTALK_ROBOT_WEBHOOK_TOKEN，无法推送审批通知")
    return await _run_dws(
        "robot_send",
        identity="webhook",
        webhook_token=token,
        text=text,
        confirm=True,
    )