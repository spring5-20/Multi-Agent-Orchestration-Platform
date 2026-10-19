"""智能问数的 LangGraph 工作流。

链路：schema（内省表结构）→ generate（生成 SQL）→ guard（只读守卫）
     → execute（只读执行）→ answer（中文结论）；守卫拒绝或执行报错时回到
     generate 自修正，最多重试 BI_SQL_MAX_RETRIES 次，仍失败则 give_up。

llm 与 executor 均可注入，便于测试时完全离线运行。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import SecretStr

from ...config import settings
from .executor import execute_readonly
from .guard_sql import guard
from .prompts import (
    ANSWER_SYSTEM_PROMPT,
    SQL_SYSTEM_PROMPT,
    build_answer_prompt,
    build_chart,
    build_sql_prompt,
    format_history,
)
from .schema import get_schema, render_schema
from .state import BiState

logger = logging.getLogger(__name__)

#: 只读执行器签名：(sql) -> (columns, rows)
SqlExecutor = Callable[[str], Awaitable[tuple[list[str], list[dict[str, Any]]]]]

#: markdown 代码块围栏（三个反引号）；单独定义便于清理模型输出。
_FENCE_MARK = "```"
_FENCE = re.compile(r"^\s*" + _FENCE_MARK + r"(?:sql)?|\s*" + _FENCE_MARK + r"\s*$", re.IGNORECASE)


def _build_llm() -> ChatOpenAI:
    """创建问数用的对话模型客户端（低温度，保证 SQL 稳定）。

    默认复用 DEEPSEEK_* 配置；配置了 BI_LLM_* 时改用它们指向的
    OpenAI 兼容端点（换供应商或走内网网关时无需改代码）。
    """
    model = settings.BI_LLM_MODEL or settings.DEEPSEEK_MODEL
    base_url = settings.BI_LLM_BASE_URL or settings.DEEPSEEK_BASE_URL
    api_key = settings.BI_LLM_API_KEY or settings.DEEPSEEK_API_KEY
    if not api_key or not model or not base_url:
        raise RuntimeError(
            "问数模型配置不完整，请配置 DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL，"
            "或改用 BI_LLM_API_KEY / BI_LLM_MODEL / BI_LLM_BASE_URL"
        )
    return ChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url,
        temperature=settings.BI_TEMPERATURE,
        timeout=60,
    )


def clean_sql(text: str) -> str:
    """清理大模型输出：去掉 markdown 代码块、语言标注与末尾分号，只留一条语句。"""
    value = _FENCE.sub("", text.strip())
    if _FENCE_MARK in value:
        parts = [part for part in value.split(_FENCE_MARK) if part.strip()]
        value = parts[0].strip() if parts else value
    if value.lower().startswith("sql\n"):
        value = value[4:]
    return value.strip().rstrip(";").strip()


def _llm_error_hint(exc: Exception) -> str:
    """把模型调用异常转成可操作的中文提示（鉴权失败单独提示配置项）。"""
    detail = f"{type(exc).__name__}: {exc}"
    if "Authentication" in detail or "401" in detail:
        return "模型鉴权失败，请检查 DEEPSEEK_API_KEY（或 BI_LLM_API_KEY / BI_LLM_BASE_URL / BI_LLM_MODEL）配置是否有效"
    return f"模型调用失败：{detail[:200]}"


def _fallback_answer(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """大模型解读失败时的兜底结论（保证接口始终有回答）。"""
    if not rows:
        return "没有查到符合条件的数据。"
    preview = "，".join(f"{column}={rows[0].get(column)}" for column in columns[:4])
    return f"共查询到 {len(rows)} 行结果，首行：{preview}。"


def _route_after_guard(state: BiState) -> str:
    """守卫之后：通过则执行，被拒则（未超上限时）回炉重写。"""
    if not state.get("error"):
        return "execute"
    if state.get("attempt", 0) <= settings.BI_SQL_MAX_RETRIES:
        return "retry"
    return "give_up"


def _route_after_execute(state: BiState) -> str:
    """执行之后：成功则解读，失败则（未超上限时）回炉重写。"""
    if not state.get("error"):
        return "answer"
    if state.get("attempt", 0) <= settings.BI_SQL_MAX_RETRIES:
        return "retry"
    return "give_up"


def build_bi_graph(
    *,
    llm: Any | None = None,
    executor: SqlExecutor | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译问数图。

    :param llm: 可选注入的大模型客户端（测试用假模型），缺省用 DeepSeek。
    :param executor: 可选注入的只读执行器，缺省走真实数据库。
    :param checkpointer: 可选检查点，用于跨请求累积多轮对话历史。
    """
    if llm is None:
        llm = _build_llm()
    if executor is None:
        executor = execute_readonly

    async def schema_node(state: BiState) -> dict[str, Any]:
        """内省数据库 catalog，渲染成注入提示词的表结构文本。"""
        schema = await get_schema()
        return {"schema_text": render_schema(schema)}

    async def generate_node(state: BiState) -> dict[str, Any]:
        """生成（或修正）SQL。"""
        prompt = build_sql_prompt(
            question=state.get("question", ""),
            schema_text=state.get("schema_text", ""),
            history_text=format_history(state.get("messages"), settings.BI_HISTORY_MESSAGES),
            feedback=state.get("feedback"),
        )
        try:
            response = await llm.ainvoke([SystemMessage(content=SQL_SYSTEM_PROMPT), HumanMessage(content=prompt)])
        except Exception as exc:  # noqa: BLE001 - 鉴权 / 网络类错误不必重试，直接给出可操作提示
            raise RuntimeError(_llm_error_hint(exc)) from exc
        return {
            "sql": clean_sql(str(response.content)),
            "attempt": state.get("attempt", 0) + 1,
            "feedback": None,
            "error": None,
        }

    async def guard_node(state: BiState) -> dict[str, Any]:
        """只读守卫：拒绝写操作 / 多语句 / 敏感列 / 系统 catalog，并夹逼 LIMIT。"""
        result = guard(state.get("sql") or "")
        if result.ok:
            return {"sql": result.sql, "error": None, "feedback": None}
        logger.warning("智能问数 SQL 被守卫拒绝: %s", result.error)
        return {"error": result.error, "feedback": f"上一版 SQL 被安全守卫拒绝：{result.error}"}

    async def execute_node(state: BiState) -> dict[str, Any]:
        """在只读事务中执行 SQL。"""
        try:
            columns, rows = await executor(state.get("sql") or "")
        except Exception as exc:  # noqa: BLE001 - 执行失败交给自修正重试
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("智能问数 SQL 执行失败: %s", message)
            return {"error": message, "feedback": f"上一次 SQL 执行失败：{message}", "columns": [], "rows": []}
        return {"columns": columns, "rows": rows, "error": None, "feedback": None}

    async def answer_node(state: BiState) -> dict[str, Any]:
        """结合问题、SQL 与结果生成中文结论，并推导图表配置。"""
        columns = list(state.get("columns") or [])
        rows = list(state.get("rows") or [])
        sql = state.get("sql") or ""
        chart = build_chart(columns, rows) if settings.BI_CHART_ENABLED else None
        prompt = build_answer_prompt(
            question=state.get("question", ""),
            sql=sql,
            columns=columns,
            rows=rows,
        )
        try:
            response = await llm.ainvoke([SystemMessage(content=ANSWER_SYSTEM_PROMPT), HumanMessage(content=prompt)])
            answer = str(response.content).strip() or _fallback_answer(rows, columns)
        except Exception as exc:  # noqa: BLE001 - 解读失败时用兜底结论，不丢结果
            logger.warning("智能问数结论生成失败，使用兜底文案: %s", exc)
            answer = _fallback_answer(rows, columns)
        return {"answer": answer, "chart": chart, "error": None, "messages": [AIMessage(content=answer)]}

    async def give_up_node(state: BiState) -> dict[str, Any]:
        """重试耗尽后的兜底：把错误原因转成可读回答，并写入对话历史。"""
        error = state.get("error") or "未知错误"
        reason = state.get("feedback") or error
        answer = f"没能完成这次问数，原因是：{reason}。可以换个问法，或先刷新表结构后重试。"
        return {"answer": answer, "chart": None, "messages": [AIMessage(content=answer)]}

    builder = StateGraph(BiState)
    builder.add_node("schema", schema_node)
    builder.add_node("generate", generate_node)
    builder.add_node("guard", guard_node)
    builder.add_node("execute", execute_node)
    builder.add_node("answer", answer_node)
    builder.add_node("give_up", give_up_node)

    builder.add_edge(START, "schema")
    builder.add_edge("schema", "generate")
    builder.add_edge("generate", "guard")
    builder.add_conditional_edges(
        "guard",
        _route_after_guard,
        {"execute": "execute", "retry": "generate", "give_up": "give_up"},
    )
    builder.add_conditional_edges(
        "execute",
        _route_after_execute,
        {"answer": "answer", "retry": "generate", "give_up": "give_up"},
    )
    builder.add_edge("answer", END)
    builder.add_edge("give_up", END)
    return builder.compile(checkpointer=checkpointer)
