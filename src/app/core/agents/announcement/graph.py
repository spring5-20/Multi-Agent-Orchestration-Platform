"""对外公告发布审批智能体的 LangGraph 工作流。

流程：START → check（风险分流）→ human_review（人工审批，interrupt 暂停）
     → publish（审批通过后真发钉钉群公告）→ END

低风险公告绕过人工审批直接发布；高风险公告在 human_review 用 interrupt 暂停，
等待人工通过 REST 回填 approve / edit / reject / cancel 后恢复执行。
"""

import asyncio
import logging
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ...config import settings
from .dingtalk_channel import notify_pending_approval, publish_notice_to_group
from .state import AnnouncementState

logger = logging.getLogger(__name__)

_graph: Any | None = None
_lock = asyncio.Lock()


def _has_sensitive(content: str) -> bool:
    """内容命中风险关键词时强制升级为人工审批。"""
    keywords = [item.strip() for item in settings.ANNOUNCEMENT_RISK_KEYWORDS.split(",") if item.strip()]
    return any(keyword in content for keyword in keywords)


def _check_node(state: AnnouncementState) -> dict[str, Any]:
    """检查风险等级：低风险自动批准，高风险转入人工审批。"""
    content = state.get("content", "")
    if state.get("risk_level") == "low" and not _has_sensitive(content):
        return {"approved": True, "status": "approved", "approval_note": "低风险公告自动发布"}
    return {"approved": False, "status": "pending_approval"}


async def _human_review_node(state: AnnouncementState) -> dict[str, Any]:
    """人工审批节点：推送通知后暂停，等待人工回填决策。"""
    if settings.ANNOUNCEMENT_NOTIFY_ENABLED:
        try:
            await notify_pending_approval(state)
        except Exception as exc:  # noqa: BLE001
            # 通知失败不阻断审批流，仅在日志记录
            logger.warning("审批通知推送失败: %s", exc)

    decision = interrupt(
        {
            "question": f"是否发布公告《{state.get('title', '')}》？",
            "announcement_id": state.get("announcement_id"),
            "title": state.get("title"),
            "content": state.get("content"),
            "risk_level": state.get("risk_level"),
        }
    )

    if not isinstance(decision, dict):
        decision = {"action": str(decision)}
    action = decision.get("action")

    if action == "approve":
        return {"approved": True, "status": "approved", "approval_note": decision.get("note") or "人工批准"}
    if action == "edit":
        updates: dict[str, Any] = {"approved": True, "status": "approved", "approval_note": "人工编辑后发布"}
        if decision.get("title"):
            updates["title"] = decision["title"]
        if decision.get("content"):
            updates["content"] = decision["content"]
        return updates
    if action == "reject":
        return {"approved": False, "status": "rejected", "approval_note": decision.get("note") or "人工拒绝"}
    return {"approved": False, "status": "canceled", "approval_note": "人工取消"}


async def _publish_node(state: AnnouncementState) -> dict[str, Any]:
    """发布节点：审批通过后真发钉钉群公告。"""
    if not state.get("approved"):
        return {}
    try:
        await publish_notice_to_group(state.get("title", ""), state.get("content", ""))
        return {"status": "published"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("钉钉群公告发布失败: %s", exc)
        return {"status": "publish_failed", "approval_note": f"发布失败：{exc}"}


def _route_after_check(state: AnnouncementState) -> str:
    """check 之后：低风险直接发布，高风险进入人工审批。"""
    if state.get("status") == "approved":
        return "publish"
    return "human_review"


def build_graph(checkpointer: Any = None) -> Any:
    """构建并编译公告审批工作流图。"""
    builder = StateGraph(AnnouncementState)
    builder.add_node("check", _check_node)
    builder.add_node("human_review", _human_review_node)
    builder.add_node("publish", _publish_node)
    builder.add_edge(START, "check")
    builder.add_conditional_edges(
        "check",
        _route_after_check,
        {"publish": "publish", "human_review": "human_review"},
    )
    builder.add_edge("human_review", "publish")
    builder.add_edge("publish", END)
    return builder.compile(checkpointer=checkpointer)


async def get_graph() -> Any:
    """获取编译好的图单例，首次调用时异步初始化检查点。"""
    global _graph
    if _graph is not None:
        return _graph
    async with _lock:
        if _graph is not None:
            return _graph
        from .runtime import get_checkpointer

        checkpointer = await get_checkpointer()
        _graph = build_graph(checkpointer=checkpointer)
        return _graph