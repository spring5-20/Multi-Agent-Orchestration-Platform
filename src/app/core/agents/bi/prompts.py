"""智能问数的提示词模板与结果解读辅助。

把「生成 SQL」与「解读结果」两段提示词拆成常量，再把动态内容拼进用户消息；
图表则根据结果列自动推导，避免额外一次大模型调用与不稳定的 JSON 解析。
"""

from __future__ import annotations

import json
from typing import Any

SQL_SYSTEM_PROMPT = (
    "你是一名严谨的数据分析助手，负责把用户的中文问题转换为一条 PostgreSQL 只读查询（SELECT）。"
    "你会先阅读给定的数据库表结构，理解每张表的用途与列含义，再生成 SQL。"
    "严格遵守以下规则：\n"
    "1. 只生成一条 SELECT（或 WITH ... SELECT）查询，禁止任何写操作、DDL 或 DCL。\n"
    "2. 只使用表结构中存在的表与列，不要臆造列名；列名与表名请使用原始大小写。\n"
    "3. 对金额、数量等数值列使用 SUM / AVG / COUNT 等聚合时，注意 GROUP BY 与 ORDER BY。\n"
    "4. 结果不确定顺序时请自行添加 ORDER BY 或 LIMIT，避免返回海量数据。\n"
    "5. 如果提供了此前的对话历史，请结合它理解指代（如「那上周呢」「销量最高的呢」）。\n"
    "6. 只输出 SQL 本身，不要输出任何解释、注释或 markdown 代码块。"
)

ANSWER_SYSTEM_PROMPT = (
    "你是一名数据分析师，请根据「用户问题 + 执行的 SQL + 查询结果」用简体中文给出简洁结论。\n"
    "要求：\n"
    "1. 先直接回答问题，把关键数字说清楚（必要时给出单位）；\n"
    "2. 结果为空时说明「没有查到符合条件的数据」，不要编造数字；\n"
    "3. 不要重复贴 SQL，也不要输出 JSON 或代码块；\n"
    "4. 最多 4 句话，可用分点。"
)


def format_history(messages: list[Any] | None, limit: int = 6) -> str:
    """把最近若干条对话消息渲染成紧凑文本，用于多轮指代。"""
    if not messages:
        return "（无）"
    lines: list[str] = []
    for message in messages[-max(limit, 0) :]:
        role = "用户" if message.__class__.__name__ == "HumanMessage" else "助手"
        content = str(getattr(message, "content", "")).strip()
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines) if lines else "（无）"


def build_sql_prompt(
    *,
    question: str,
    schema_text: str,
    history_text: str = "（无）",
    feedback: str | None = None,
) -> str:
    """拼装「生成 SQL」的用户消息。"""
    parts = [
        "【数据库表结构】",
        schema_text or "（未能读取到表结构）",
        "",
        "【对话历史】",
        history_text,
        "",
        "【当前问题】",
        question,
    ]
    if feedback:
        parts += [
            "",
            "【上一次尝试的失败原因，请修正后重新生成】",
            feedback,
        ]
    parts += ["", "请只输出一条 PostgreSQL 只读 SQL。"]
    return "\n".join(parts)


def render_rows(columns: list[str], rows: list[dict[str, Any]], limit: int = 30) -> str:
    """把查询结果渲染成紧凑文本，供解读节点使用。"""
    if not columns:
        return "（无结果）"
    head = " | ".join(columns)
    lines = [head, "-" * len(head)]
    for row in rows[:limit]:
        lines.append(" | ".join(str(row.get(column, "")) for column in columns))
    if len(rows) > limit:
        lines.append(f"...（共 {len(rows)} 行，仅展示前 {limit} 行）")
    return "\n".join(lines)


def build_answer_prompt(
    *,
    question: str,
    sql: str,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> str:
    """拼装「解读结果」的用户消息。"""
    return "\n".join(
        [
            f"【用户问题】{question}",
            "",
            "【执行的 SQL】",
            sql,
            "",
            f"【查询结果】共 {len(rows)} 行",
            render_rows(columns, rows),
            "",
            "请给出结论。",
        ]
    )


def build_chart(columns: list[str], rows: list[dict[str, Any]], max_items: int = 20) -> dict[str, Any] | None:
    """根据结果列自动推导一个柱状图配置；无法推导时返回 None。

    规则：取第一个数值列作为数值轴、取第一个非数值列作为类目轴，
    最多展示 max_items 条；两份数据都非空才生成图表。
    """
    if len(columns) < 2 or not rows:
        return None

    def _is_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    numeric_column = next(
        (column for column in columns if all(_is_number(row.get(column)) or row.get(column) is None for row in rows)),
        None,
    )
    label_column = next((column for column in columns if column != numeric_column), None)
    if numeric_column is None or label_column is None:
        return None

    sliced = rows[:max_items]
    x_axis = [str(row.get(label_column, "")) for row in sliced]
    data = [row.get(numeric_column) or 0 for row in sliced]
    if not x_axis or not any(data):
        return None
    return {
        "type": "bar",
        "x_axis": x_axis,
        "series": [{"name": numeric_column, "data": data}],
    }


def chart_to_text(chart: dict[str, Any] | None) -> str:
    """把图表配置序列化为日志友好的一行文本。"""
    return json.dumps(chart, ensure_ascii=False) if chart else ""
