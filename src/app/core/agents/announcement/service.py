"""对外公告发布审批智能体对外服务：提交、查询与人工回填。"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command
from sqlalchemy import func, select

from ...db.database import local_session
from ...db.models import Announcement, ApprovalRecord
from .graph import get_graph

logger = logging.getLogger(__name__)

_FINAL_STATUSES = ("published", "publish_failed", "rejected", "canceled")


def _utcnow() -> datetime:
    """返回无时区信息的 UTC 当前时间，与 ``DateTime``（无时区）列匹配。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _thread_config(thread_id: str) -> dict[str, Any]:
    """构造 LangGraph 的 configurable 配置。"""
    return {"configurable": {"thread_id": thread_id}}


@dataclass
class AnnouncementView:
    """公告的对外视图。"""

    id: int
    thread_id: str
    title: str
    content: str
    risk_level: str
    status: str
    submitter: str | None
    approver: str | None
    approval_note: str | None
    published_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, model: Announcement) -> "AnnouncementView":
        """从 ORM 模型构造视图。"""
        return cls(
            id=model.id,
            thread_id=model.thread_id,
            title=model.title,
            content=model.content,
            risk_level=model.risk_level,
            status=model.status,
            submitter=model.submitter,
            approver=model.approver,
            approval_note=model.approval_note,
            published_at=model.published_at,
            created_at=model.created_at,
        )


async def submit(
    title: str,
    content: str,
    risk_level: str = "high",
    submitter: str | None = None,
) -> AnnouncementView:
    """提交一条公告：图执行到人工审批点（暂停）或直接发布。"""
    thread_id = f"announcement:{uuid.uuid4().hex}"
    async with local_session() as session:
        model = Announcement(
            thread_id=thread_id,
            title=title,
            content=content,
            risk_level=risk_level,
            status="submitted",
            submitter=submitter,
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)
        announcement_id = model.id

    graph = await get_graph()
    initial_state = {
        "announcement_id": announcement_id,
        "thread_id": thread_id,
        "title": title,
        "content": content,
        "risk_level": risk_level,
        "submitter": submitter,
        "approved": False,
        "status": "submitted",
    }
    result = await graph.ainvoke(initial_state, config=_thread_config(thread_id))
    if result.get("__interrupt__"):
        # 图在 human_review 的 interrupt 处暂停，等待人工回填
        status = "pending_approval"
        note = None
    else:
        status = result.get("status") or "submitted"
        note = result.get("approval_note")

    published_at = _utcnow() if status == "published" else None
    async with local_session() as session:
        model = await session.get(Announcement, announcement_id)
        if model is None:
            raise RuntimeError(f"公告 {announcement_id} 不存在")
        model.status = status
        if note:
            model.approval_note = note
        model.published_at = published_at
        await session.commit()
        await session.refresh(model)
        return AnnouncementView.from_model(model)


async def get(announcement_id: int) -> AnnouncementView:
    """按 id 查询单条公告。"""
    async with local_session() as session:
        model = await session.get(Announcement, announcement_id)
        if model is None:
            raise ValueError(f"公告 {announcement_id} 不存在")
        return AnnouncementView.from_model(model)


async def list_announcements(page: int = 1, page_size: int = 20) -> tuple[list[AnnouncementView], int]:
    """分页查询公告列表。"""
    async with local_session() as session:
        count_stmt = select(func.count()).select_from(Announcement)
        total = await session.scalar(count_stmt)
        list_stmt = (
            select(Announcement).order_by(Announcement.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
        items = (await session.scalars(list_stmt)).all()
        return [AnnouncementView.from_model(item) for item in items], total or 0


async def respond(
    announcement_id: int,
    action: str,
    approver: str,
    *,
    title: str | None = None,
    content: str | None = None,
    note: str | None = None,
) -> AnnouncementView:
    """人工回填审批结果（approve / edit / reject / cancel），恢复图执行并落审计。"""
    async with local_session() as session:
        model = await session.get(Announcement, announcement_id)
        if model is None:
            raise ValueError(f"公告 {announcement_id} 不存在")
        if model.status != "pending_approval":
            raise ValueError(f"公告 {announcement_id} 当前状态为 {model.status}，不可审批")
        thread_id = model.thread_id

    resume: dict[str, Any] = {"action": action, "note": note}
    if title:
        resume["title"] = title
    if content:
        resume["content"] = content

    graph = await get_graph()
    result = await graph.ainvoke(Command(resume=resume), config=_thread_config(thread_id))
    status = result.get("status") or "submitted"
    if status not in _FINAL_STATUSES:
        logger.warning("回填后图未进入终态，实际状态为 %s", status)

    async with local_session() as session:
        model = await session.get(Announcement, announcement_id)
        if model is None:
            raise RuntimeError(f"公告 {announcement_id} 不存在")
        # 编辑响应会更新标题/正文，同步落库
        if action == "edit":
            if title:
                model.title = title
            if content:
                model.content = content
        model.status = status
        model.approver = approver
        model.approval_note = result.get("approval_note") or note
        model.published_at = _utcnow() if status == "published" else None
        session.add(
            ApprovalRecord(
                announcement_id=announcement_id,
                approver=approver,
                action=action,
                note=note,
            )
        )
        await session.commit()
        await session.refresh(model)
        return AnnouncementView.from_model(model)