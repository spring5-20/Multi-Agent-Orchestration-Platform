"""规划与反思：LangGraph 实现 Reflection 与 Replanning。

Reflection（反思）指提示 LLM 观察其过去的步骤（以及来自工具/环境的观察），
评估输出的质量：生成初稿 → 批评 → 修订。

Replanning（重规划）：当执行失败时，Agent 需要重新规划路径，
失败后自动重新规划，最多重试 3 次。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import SecretStr

from ...config import settings

# ---------------- LLM ----------------

_llm: ChatOpenAI | None = None


def _build_llm() -> ChatOpenAI:
    """创建 DeepSeek 大模型客户端（OpenAI 兼容接口）。"""
    model = settings.DEEPSEEK_MODEL
    api_key = settings.DEEPSEEK_API_KEY
    base_url = settings.DEEPSEEK_BASE_URL
    if not model or not api_key or not base_url:
        raise RuntimeError("DeepSeek 配置不完整，请检查 DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL")

    return ChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url,
        temperature=0.7,
        timeout=120,
    )


def _get_llm() -> ChatOpenAI:
    """惰性创建并缓存 LLM 实例。"""
    global _llm
    if _llm is None:
        _llm = _build_llm()
    return _llm


# ---------------- Reflection：生成初稿 → 批评 → 修订 ----------------


class ReflectionState(TypedDict):
    task: str
    draft: str
    critique: str
    final: str


def build_reflection_graph(llm: Any | None = None) -> Any:
    """构建 Reflection 图：generate → critique → revise。

    :param llm: 可选注入的大模型客户端（测试用假模型），缺省使用 DeepSeek。
    """
    if llm is None:
        llm = _get_llm()

    def generate_draft(state: ReflectionState) -> dict:
        """生成初稿"""
        draft = llm.invoke(f"请完成以下任务：{state['task']}")
        return {"draft": draft.content}

    def critique_draft(state: ReflectionState) -> dict:
        """批评初稿"""
        prompt = f"""
任务：{state['task']}
初稿：{state['draft']}

请指出初稿的问题，给出改进建议。
"""
        critique = llm.invoke(prompt)
        return {"critique": critique.content}

    def revise_draft(state: ReflectionState) -> dict:
        """根据批评修订"""
        prompt = f"""
任务：{state['task']}
初稿：{state['draft']}
批评：{state['critique']}

请根据批评修订初稿。
"""
        final = llm.invoke(prompt)
        return {"final": final.content}

    # 构建图
    builder = StateGraph(ReflectionState)
    builder.add_node("generate", generate_draft)
    builder.add_node("critique", critique_draft)
    builder.add_node("revise", revise_draft)
    builder.add_edge(START, "generate")
    builder.add_edge("generate", "critique")
    builder.add_edge("critique", "revise")
    builder.add_edge("revise", END)
    return builder.compile()


def run_reflection(task: str, *, llm: Any | None = None) -> str:
    """运行一次完整的 Reflection 流程，返回修订后的最终结果。"""
    graph = build_reflection_graph(llm)
    result = graph.invoke({"task": task, "draft": "", "critique": "", "final": ""})
    return str(result.get("final", ""))


# ---------------- Replanning：失败自动重规划（最多重试 3 次） ----------------


class TaskState(TypedDict):
    task: str
    plan: str
    status: str  # pending | success | failed
    result: str
    error: str
    retry_count: int


#: 执行器签名：输入当前状态（含任务与计划），返回状态更新片段。
Executor = Callable[[TaskState], dict[str, Any]]

#: 最大重规划次数（超过后直接结束，不再重试）。
MAX_RETRIES = 3


def should_replan(state: TaskState) -> str:
    """判断是否需要重规划"""
    if state["status"] == "failed" and state["retry_count"] < MAX_RETRIES:
        return "replan"
    return "end"


def build_replanning_graph(executor: Executor | None = None, llm: Any | None = None) -> Any:
    """构建带重规划的任务执行图：plan → execute →(failed?)→ replan → execute …

    :param executor: 可选注入的任务执行器（测试或自定义执行逻辑），
        返回 ``{"status": "success"|"failed", "result": ..., "error": ...}``；
        缺省使用 LLM 按当前计划执行任务。
    :param llm: 可选注入的大模型客户端（用于制定计划与默认执行），缺省使用 DeepSeek。
    """
    if llm is None:
        llm = _get_llm()
    if executor is None:
        executor = _llm_executor(llm)

    def plan_task(state: TaskState) -> dict:
        """首次进入时制定初始计划（已有计划则跳过）。"""
        if state.get("plan"):
            return {}
        prompt = f"请为以下任务制定一个可执行的步骤计划：{state['task']}"
        plan = llm.invoke(prompt)
        return {"plan": plan.content}

    def execute(state: TaskState) -> dict:
        """按当前计划执行任务。"""
        return executor(state)

    def replan(state: TaskState) -> dict:
        """重新规划"""
        prompt = f"""
之前的计划失败了。
任务：{state['task']}
失败原因：{state['error']}
请制定新的计划。
"""
        new_plan = llm.invoke(prompt)
        return {"plan": new_plan.content, "retry_count": state["retry_count"] + 1}

    builder = StateGraph(TaskState)
    builder.add_node("plan", plan_task)
    builder.add_node("execute", execute)
    builder.add_node("replan", replan)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    builder.add_conditional_edges("execute", should_replan, {"replan": "replan", "end": END})
    builder.add_edge("replan", "execute")
    return builder.compile()


def run_task_with_replanning(
    task: str,
    *,
    executor: Executor | None = None,
    llm: Any | None = None,
) -> TaskState:
    """执行任务：失败自动重新规划并重试，最多重试 3 次，返回最终状态。"""
    graph = build_replanning_graph(executor=executor, llm=llm)
    initial: TaskState = {"task": task, "plan": "", "status": "pending", "result": "", "error": "", "retry_count": 0}
    result: TaskState = graph.invoke(initial)
    return result


def _llm_executor(llm: Any) -> Executor:
    """默认执行器：让大模型按当前计划完成任务，异常视为执行失败。"""

    def execute(state: TaskState) -> dict[str, Any]:
        prompt = f"请按照以下计划完成任务，直接给出结果。\n任务：{state['task']}\n计划：{state['plan']}"
        try:
            answer = llm.invoke(prompt)
            return {"status": "success", "result": str(answer.content), "error": ""}
        except Exception as exc:  # noqa: BLE001 - 执行失败交由重规划处理。
            return {"status": "failed", "result": "", "error": f"{type(exc).__name__}: {exc}"}

    return execute
