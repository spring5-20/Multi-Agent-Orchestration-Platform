"""钉协助手智能体的 LangChain 工具封装，供 Agent 与直连接口调用。

所有工具最终都调用 :func:`cli.run_dws`，由 `cli._ACTIONS` 白名单统一做命令校验与写操作开关控制。

**写操作的确认语义**：写类工具不再暴露 `confirm` 参数（模型无法自己决定"已获用户确认"），
真实执行与否由调用方通过运行配置注入的 `dingtalk_confirmed` 标记决定：

- 未被用户确认（默认）：`run_dws` 追加 `--dry-run`，只在本地预演，钉钉侧不产生任何改动；
- 用户已确认（编排器确认闭环 / 前端显式确认）：追加 `--yes` 真实执行。
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from ...config import settings
from .cli import run_dws

# ---------------- 确认标记 ----------------


def _confirmed(config: RunnableConfig | None) -> bool:
    """从运行配置里读取「用户已确认」标记。

    该标记只能由调用方（编排器确认闭环、显式确认的接口）注入，模型无法通过工具参数伪造。
    """
    if not config:
        return False
    configurable: dict[str, Any] = config.get("configurable") or {}
    return bool(configurable.get("dingtalk_confirmed"))


# ---------------- 通讯录 ----------------


@tool
def search_contact(query: str) -> str:
    """在钉钉通讯录中搜索联系人，返回 userId、姓名、部门、职位、手机号等信息。

    当用户要找某人的联系方式、部门、职位，或需要先拿到 userId 再发消息/指派任务时使用。

    Args:
        query: 姓名、花名或关键词，例如「张三」。
    """
    return run_dws("contact_search", query=query)


# ---------------- 日历 ----------------


@tool
def list_calendar() -> str:
    """查询今天的钉钉日程安排。

    当用户询问今天有什么会、今天有什么安排时使用。
    """
    return run_dws("calendar_list")


@tool
def create_calendar(
    title: str,
    start: str,
    end: str,
    attendees: str | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> str:
    """在钉钉日历中创建日程/会议，并可按姓名邀请参会人。

    当用户要安排会议、创建日程提醒时使用。
    真实执行需要用户确认（由调用方注入确认标记）；未确认时只做预演，钉钉侧不会产生改动。

    Args:
        title: 日程标题，例如「xxx 项目评审会」。
        start: 开始时间（ISO8601），例如「2026-09-23T14:00:00+08:00」。
        end: 结束时间（ISO8601）。
        attendees: 参会人姓名，逗号分隔，例如「张三,李四」。
    """
    return run_dws(
        "calendar_create",
        title=title,
        start=start,
        end=end,
        attendees=attendees,
        confirm=_confirmed(config),
    )


# ---------------- 待办 ----------------


@tool
def list_todos(include_done: bool = False, priority: str | None = None) -> str:
    """查询我的钉钉待办列表。

    当用户询问自己的待办、未完成任务时使用。

    Args:
        include_done: 是否包含已完成的待办，默认 False（只查未完成）。
        priority: 优先级过滤，逗号分隔，可选值 10/20/30/40，例如「40,30」。
    """
    status = "true" if include_done else "false"
    return run_dws("todo_list", status=status, priority=priority)


@tool
def create_todo(task: str, at: str | None = None, config: RunnableConfig = None) -> str:  # type: ignore[assignment]
    """给自己创建一条待办。

    当用户说「帮我记个待办」「提醒我 …」时使用。
    真实执行需要用户确认（由调用方注入确认标记）；未确认时只做预演。

    Args:
        task: 待办标题/内容，例如「交周报」。
        at: 截止时间（ISO8601），例如「2026-09-25T18:00:00+08:00」。
    """
    return run_dws("todo_remind", task=task, at=at, confirm=_confirmed(config))


@tool
def assign_todo(
    task: str,
    to: str,
    due: str | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> str:
    """给某位同事创建并指派一条待办。

    当用户说「给李四派个活」「让张三做 …」时使用。
    真实执行需要用户确认（由调用方注入确认标记）；未确认时只做预演。

    Args:
        task: 待办标题/内容。
        to: 执行人姓名/花名，例如「李四」。
        due: 截止时间（ISO8601），可选。
    """
    return run_dws("todo_assign", task=task, to=to, due=due, confirm=_confirmed(config))


# ---------------- 消息 / 机器人 ----------------


@tool
def send_message(to: str, content: str, config: RunnableConfig = None) -> str:  # type: ignore[assignment]
    """给某位同事发一条钉钉单聊消息。

    当用户说「给张三发个消息」「告诉李四 …」时使用。
    真实发送需要用户确认（由调用方注入确认标记）；未确认时只做预演，不会真的发出去。

    Args:
        to: 收件人姓名/花名，例如「张三」。
        content: 消息内容，支持 Markdown。
    """
    return run_dws("message_send", to=to, content=content, confirm=_confirmed(config))


@tool
def send_group_message(group: str, content: str, config: RunnableConfig = None) -> str:  # type: ignore[assignment]
    """给某个钉钉群发一条消息。

    当用户说「在项目群里发 …」「通知 XX 群 …」时使用。
    真实发送需要用户确认（由调用方注入确认标记）；未确认时只做预演。

    Args:
        group: 群名称，例如「项目冲刺」。
        content: 消息内容，支持 Markdown。
    """
    return run_dws("group_message_send", group=group, content=content, confirm=_confirmed(config))


@tool
def send_robot_message(content: str, config: RunnableConfig = None) -> str:  # type: ignore[assignment]
    """通过群自定义机器人（Webhook）向机器人所在群发送消息。

    当用户要求用机器人推送通知时使用。
    真实发送需要用户确认（由调用方注入确认标记）；未确认时只做预演。

    Args:
        content: 消息内容（需包含机器人安全设置的关键词，否则会被拒收）。
    """
    token = settings.DINGTALK_ROBOT_WEBHOOK_TOKEN
    if not token:
        raise RuntimeError("未配置 DINGTALK_ROBOT_WEBHOOK_TOKEN，请在 src/.env 中配置群机器人 Webhook token")
    return run_dws("robot_send", identity="webhook", webhook_token=token, text=content, confirm=_confirmed(config))


# ---------------- 群搜索 / 群公告 ----------------


@tool
def search_group(query: str) -> str:
    """按关键词搜索钉钉群聊，返回群名称与 openConversationId。

    当用户要往某个群发消息/公告、但只有群名关键词时，先用本工具解析出 openConversationId。

    Args:
        query: 群名称关键词，例如「师生群」。
    """
    return run_dws("group_search", query=query)


@tool
def publish_notice(
    conversation_id: str,
    content: str,
    sticky: bool = False,
    send_ding: bool = False,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> str:
    """在指定钉钉群聊中发布一条群公告（正文为 Markdown 格式）。

    当用户要求在群里发公告时使用。conversation_id 是群的 openConversationId，可先用 search_group 解析。
    真实发布需要用户确认（由调用方注入确认标记）；未确认时只做预演。

    Args:
        conversation_id: 群聊 openConversationId，例如「cidFQ3a+0mk0am0xbTzkRx2Vg==」。
        content: 公告正文，Markdown 格式。
        sticky: 是否置顶，默认 False。
        send_ding: 是否发 DING 提醒，默认 False。
    """
    return run_dws(
        "notice_create",
        conversation_id=conversation_id,
        content=content,
        sticky=sticky,
        send_ding=send_ding,
        confirm=_confirmed(config),
    )


# ---------------- DING 消息 ----------------


@tool
def send_ding(
    users: str,
    content: str,
    method: str = "app",
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> str:
    """发送钉钉 DING 强提醒消息（打扰性强，谨慎使用）。

    当用户要求强提醒、紧急通知时使用。
    真实发送需要用户确认（由调用方注入确认标记）；未确认时只做预演。

    Args:
        users: 接收人 openDingTalkId 列表，逗号分隔（可先用 search_contact 解析）。
        content: 提醒内容。
        method: 提醒方式，可选 app/sms/call，默认 app。
    """
    return run_dws("ding_send", users=users, content=content, method=method, confirm=_confirmed(config))


# ---------------- 在线表格 ----------------


@tool
def create_sheet(
    name: str,
    folder: str | None = None,
    workspace: str | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> str:
    """创建一篇钉钉在线电子表格文档。

    当用户要求建表格、做数据收集时使用。
    真实创建需要用户确认（由调用方注入确认标记）；未确认时只做预演。

    Args:
        name: 表格名称，例如「Q1 数据」。
        folder: 目标文件夹 ID，可选。
        workspace: 目标知识库 ID，可选。
    """
    return run_dws("sheet_create", name=name, folder=folder, workspace=workspace, confirm=_confirmed(config))


# ---------------- 日志（日报） ----------------


@tool
def list_logs() -> str:
    """查询我收到的钉钉日志（日报）。

    当用户询问收到的日报时使用。
    """
    return run_dws("log_list")


@tool
def create_log(
    template_id: str,
    contents: str,
    to_user_ids: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> str:
    """按模板提交一份钉钉日志/日报。

    当用户要求写日报、提交日志时使用；需先获取模板 ID 与字段定义。
    真实提交需要用户确认（由调用方注入确认标记）；未确认时只做预演。

    Args:
        template_id: 日志模板 ID。
        contents: 日志内容 JSON 数组字符串，每项含 key/sort/content/contentType/type。
        to_user_ids: 接收人 userId，逗号分隔。
    """
    return run_dws(
        "log_create",
        template_id=template_id,
        contents=contents,
        to_user_ids=to_user_ids,
        confirm=_confirmed(config),
    )


# ---------------- 考勤 ----------------


@tool
def get_attendance() -> str:
    """查询我今天的钉钉考勤打卡流水。

    当用户询问今天的打卡、出勤情况时使用。
    """
    return run_dws("attendance_get")


dingtalk_tools = [
    search_contact,
    list_calendar,
    create_calendar,
    list_todos,
    create_todo,
    assign_todo,
    send_message,
    send_group_message,
    send_robot_message,
    search_group,
    publish_notice,
    send_ding,
    create_sheet,
    list_logs,
    create_log,
    get_attendance,
]
