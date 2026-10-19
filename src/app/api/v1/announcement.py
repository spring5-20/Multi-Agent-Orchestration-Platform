"""对外公告发布审批智能体 API：提交、查询与人工审批回填。"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ...core.agents.announcement.service import get, list_announcements, respond, submit

router = APIRouter(prefix="/announcements", tags=["公告审批"])


class AnnouncementCreate(BaseModel):
    """提交公告请求体。"""

    title: str = Field(..., min_length=1, max_length=255, description="公告标题")
    content: str = Field(..., min_length=1, description="公告正文（Markdown）")
    risk_level: str = Field("high", pattern="^(low|high)$", description="风险等级：low 自动发布 / high 人工审批")
    submitter: str | None = Field(None, max_length=100, description="提交人")


class AnnouncementRespond(BaseModel):
    """人工审批回填请求体。"""

    action: str = Field(..., pattern="^(approve|edit|reject|cancel)$", description="审批动作")
    approver: str = Field("审批人", min_length=1, max_length=100, description="决策人")
    title: str | None = Field(None, max_length=255, description="edit 时的新标题")
    content: str | None = Field(None, description="edit 时的新正文")
    note: str | None = Field(None, description="审批意见 / 拒绝原因")


class AnnouncementResponse(BaseModel):
    """公告响应体。"""

    model_config = ConfigDict(from_attributes=True)

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


class AnnouncementListResponse(BaseModel):
    """公告分页列表响应体。"""

    items: list[AnnouncementResponse]
    total: int
    page: int
    page_size: int


def _to_response(value: object) -> AnnouncementResponse:
    """把服务层视图转换为响应模型。"""
    return AnnouncementResponse.model_validate(value)


@router.post("", response_model=AnnouncementResponse, status_code=201, summary="提交公告")
async def submit_announcement(request: AnnouncementCreate) -> AnnouncementResponse:
    """提交一条公告，低风险直接发布，高风险暂停等待人工审批。"""
    try:
        view = await submit(request.title, request.content, request.risk_level, request.submitter)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _to_response(view)


@router.get("", response_model=AnnouncementListResponse, summary="分页查询公告")
async def list_announcements_api(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
) -> AnnouncementListResponse:
    """分页查询公告列表。"""
    items, total = await list_announcements(page, page_size)
    return AnnouncementListResponse(
        items=[_to_response(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{announcement_id}", response_model=AnnouncementResponse, summary="查询公告详情")
async def get_announcement(announcement_id: int) -> AnnouncementResponse:
    """按 id 查询单条公告。"""
    try:
        view = await get(announcement_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_response(view)


@router.post("/{announcement_id}/respond", response_model=AnnouncementResponse, summary="人工审批回填")
async def respond_announcement(announcement_id: int, request: AnnouncementRespond) -> AnnouncementResponse:
    """人工回填 approve / edit / reject / cancel，恢复图执行。"""
    try:
        view = await respond(
            announcement_id,
            request.action,
            request.approver,
            title=request.title,
            content=request.content,
            note=request.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _to_response(view)