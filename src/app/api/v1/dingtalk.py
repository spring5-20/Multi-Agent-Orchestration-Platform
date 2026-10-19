"""钉协助手智能体直连接口：把 dws CLI 的读写动作与大模型对话暴露成 REST 路由。"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...core.agents.dingtalk import agent as dingtalk_agent
from ...core.agents.dingtalk import cli as dingtalk_cli
from ...core.config import settings

router = APIRouter(prefix="/dingtalk", tags=["dingtalk"])


def _run(action: str, **params: Any) -> str:
    """执行 dws 动作，并把执行期错误转成带明确原因的 HTTP 错误。

    dws 在非交互环境里的失败以非零退出码返回（未登录、参数不合法、超时等），
    若直接抛出会变成没有原因的 500；这里统一转成 400/502，让前端能显示 CLI 的真实提示。
    """
    try:
        return dingtalk_cli.run_dws(action, **params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------- 登录状态 ----------------


@router.get("/auth/status")
def auth_status() -> dict[str, Any]:
    """查询钉钉登录状态。"""
    return dingtalk_cli.auth_status()


@router.post("/auth/login")
def auth_login() -> dict[str, Any]:
    """触发钉钉扫码登录（会在本机打开浏览器授权）。"""
    dingtalk_cli.auth_login()
    return {"triggered": True, "message": "已触发登录，请在浏览器完成授权"}


# ---------------- 运行环境 ----------------


@router.get("/health")
async def dingtalk_health() -> dict[str, Any]:
    """检查 dws CLI 是否可用。"""
    return {"dws_available": dingtalk_cli.is_available()}


# ---------------- 直连：读操作 ----------------


@router.get("/contact/search")
def contact_search(query: str) -> dict[str, Any]:
    """搜索钉钉通讯录联系人。"""
    return {"result": _run("contact_search", query=query)}


@router.get("/calendar/list")
def calendar_list() -> dict[str, Any]:
    """查询今天的钉钉日程。"""
    return {"result": _run("calendar_list")}


@router.get("/todo/list")
def todo_list(include_done: bool = False, priority: str | None = None) -> dict[str, Any]:
    """查询我的钉钉待办。"""
    status = "true" if include_done else "false"
    return {"result": _run("todo_list", status=status, priority=priority)}


@router.get("/log/list")
def log_list() -> dict[str, Any]:
    """查询我收到的钉钉日志。"""
    return {"result": _run("log_list")}


@router.get("/attendance")
def attendance() -> dict[str, Any]:
    """查询我今天的钉钉考勤打卡流水。"""
    return {"result": _run("attendance_get")}


@router.get("/group/search")
def group_search(query: str) -> dict[str, Any]:
    """按关键词搜索钉钉群聊。"""
    return {"result": _run("group_search", query=query)}


# ---------------- 直连：写操作 ----------------


class CalendarCreateRequest(BaseModel):
    """创建日程请求体。"""

    title: str
    start: str
    end: str
    attendees: str | None = None
    confirm: bool = False


class TodoCreateRequest(BaseModel):
    """给自己创建待办请求体。"""

    task: str
    at: str | None = None
    confirm: bool = False


class TodoAssignRequest(BaseModel):
    """给同事指派待办请求体。"""

    task: str
    to: str
    due: str | None = None
    confirm: bool = False


class MessageSendRequest(BaseModel):
    """发送单聊消息请求体。"""

    to: str
    content: str
    confirm: bool = False


class GroupMessageSendRequest(BaseModel):
    """发送群消息请求体。"""

    group: str
    content: str
    confirm: bool = False


class DingSendRequest(BaseModel):
    """发送 DING 请求体。"""

    users: str
    content: str
    method: str = "app"
    confirm: bool = False


class SheetCreateRequest(BaseModel):
    """创建在线表格请求体。"""

    name: str
    folder: str | None = None
    workspace: str | None = None
    confirm: bool = False


class LogCreateRequest(BaseModel):
    """提交日志请求体。"""

    template_id: str
    contents: str
    to_user_ids: str
    confirm: bool = False


class NoticeCreateRequest(BaseModel):
    """发布群公告请求体。"""

    conversation_id: str
    content: str
    sticky: bool = False
    send_ding: bool = False
    confirm: bool = False


class RobotMessageSendRequest(BaseModel):
    """发送机器人消息请求体。"""

    content: str
    confirm: bool = False


@router.post("/calendar")
def calendar_create(req: CalendarCreateRequest) -> dict[str, Any]:
    """创建钉钉日程。"""
    return {
        "result": _run(
            "calendar_create",
            title=req.title,
            start=req.start,
            end=req.end,
            attendees=req.attendees,
            confirm=req.confirm,
        )
    }


@router.post("/todo")
def todo_create(req: TodoCreateRequest) -> dict[str, Any]:
    """给自己创建钉钉待办。"""
    return {"result": _run("todo_remind", task=req.task, at=req.at, confirm=req.confirm)}


@router.post("/todo/assign")
def todo_assign(req: TodoAssignRequest) -> dict[str, Any]:
    """给同事指派钉钉待办。"""
    return {"result": _run("todo_assign", task=req.task, to=req.to, due=req.due, confirm=req.confirm)}


@router.post("/message/send")
def message_send(req: MessageSendRequest) -> dict[str, Any]:
    """发送钉钉单聊消息。"""
    return {"result": _run("message_send", to=req.to, content=req.content, confirm=req.confirm)}


@router.post("/group-message/send")
def group_message_send(req: GroupMessageSendRequest) -> dict[str, Any]:
    """发送钉钉群消息。"""
    return {"result": _run("group_message_send", group=req.group, content=req.content, confirm=req.confirm)}


@router.post("/ding/send")
def ding_send(req: DingSendRequest) -> dict[str, Any]:
    """发送钉钉 DING 强提醒。"""
    return {"result": _run("ding_send", users=req.users, content=req.content, method=req.method, confirm=req.confirm)}


@router.post("/sheet")
def sheet_create(req: SheetCreateRequest) -> dict[str, Any]:
    """创建钉钉在线表格。"""
    return {
        "result": _run("sheet_create", name=req.name, folder=req.folder, workspace=req.workspace, confirm=req.confirm)
    }


@router.post("/log")
def log_create(req: LogCreateRequest) -> dict[str, Any]:
    """提交钉钉日志。"""
    return {
        "result": _run(
            "log_create",
            template_id=req.template_id,
            contents=req.contents,
            to_user_ids=req.to_user_ids,
            confirm=req.confirm,
        )
    }


@router.post("/notice")
def notice_create(req: NoticeCreateRequest) -> dict[str, Any]:
    """发布钉钉群公告。"""
    return {
        "result": _run(
            "notice_create",
            conversation_id=req.conversation_id,
            content=req.content,
            sticky=req.sticky,
            send_ding=req.send_ding,
            confirm=req.confirm,
        )
    }


@router.post("/robot-message/send")
def robot_message_send(req: RobotMessageSendRequest) -> dict[str, Any]:
    """通过群自定义机器人（Webhook）发送消息。"""
    token = settings.DINGTALK_ROBOT_WEBHOOK_TOKEN
    if not token:
        raise HTTPException(
            status_code=502,
            detail="未配置 DINGTALK_ROBOT_WEBHOOK_TOKEN，请在 src/.env 中配置群机器人 Webhook token",
        )
    return {
        "result": _run(
            "robot_send",
            identity="webhook",
            webhook_token=token,
            text=req.content,
            confirm=req.confirm,
        )
    }


# ---------------- 智能体：流式对话 ----------------


class ChatRequest(BaseModel):
    """钉协助手智能体问答请求体。"""

    question: str


@router.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """钉协助手智能体问答（流式 SSE）：模型自动调用钉钉工具并返回结果。"""

    async def event_stream() -> Any:
        # 对话入口不做真实写操作：写类工具只会预演，真正确认请走确认闭环或 /operations 的接口
        async for event in dingtalk_agent.stream_events(req.question, confirmed=False):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield 'data: {"type": "done"}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")
