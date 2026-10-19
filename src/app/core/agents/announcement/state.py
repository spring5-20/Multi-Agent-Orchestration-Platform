"""对外公告发布审批智能体的图状态定义。"""

from typing import Any, TypedDict


class AnnouncementState(TypedDict, total=False):
    """图节点间共享的公告审批状态。"""

    announcement_id: int
    thread_id: str
    title: str
    content: str
    risk_level: str
    submitter: str | None
    approved: bool
    status: str
    approval_note: str | None
    pending_decision: dict[str, Any] | None