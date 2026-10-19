"""Reflexion（反射）：失败后生成文字反思，并注入后续尝试，避免重复犯错。

与 Replanning 的区别：

- Replanning 只根据失败原因重新制定计划；
- Reflexion 额外让 LLM 复盘「上次为什么失败、下次要避免什么」，把文字反思
  累积保存，之后每一步重规划都会带上历史反思，从而避免重复踩同一个坑。

图结构：plan → execute →(失败且未超上限)→ reflect → replan → execute …
反思次数与重规划次数一致，最多 MAX_RETRIES 次。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .graph import Executor, _get_llm, _llm_executor, should_replan


class ReflexionState(TypedDict):
    """Reflexion 任务状态：比 Replanning 多一条累积的反思列表。"""

    task: str
    plan: str
    status: str  # pending | success | failed
    result: str
    error: str
    retry_count: int
    reflections: list[str]


#: 反思器签名：输入当前状态，返回一段文字反思。
Reflector = Callable[[ReflexionState], str]


def _llm_reflector(llm: Any) -> Reflector:
    """默认反思器：让大模型复盘失败原因与下次的注意事项。"""

    def reflect(state: ReflexionState) -> str:
        prompt = (
            f"任务：{state['task']}\n"
            f"上一次计划：{state['plan']}\n"
            f"失败原因：{state['error']}\n"
            "请用一两句话反思：这次失败的根本原因是什么？下次执行应该避免什么？"
        )
        return str(llm.invoke(prompt).content)

    return reflect


def build_reflexion_graph(
    executor: Executor | None = None,
    llm: Any | None = None,
    reflector: Reflector | None = None,
) -> Any:
    """构建带反思的任务执行图：plan → execute → reflect → replan → execute …

    :param executor: 可选注入的任务执行器；缺省使用 LLM 按当前计划执行任务。
    :param llm: 可选注入的大模型客户端（制定计划与默认执行），缺省使用 DeepSeek。
    :param reflector: 可选注入的反思器，缺省使用 LLM 生成文字反思。
    """
    if llm is None:
        llm = _get_llm()
    if executor is None:
        executor = _llm_executor(llm)
    if reflector is None:
        reflector = _llm_reflector(llm)

    def plan_task(state: ReflexionState) -> dict:
        """首次进入时制定初始计划（已有计划则跳过）。"""
        if state.get("plan"):
            return {}
        plan = llm.invoke(f"请为以下任务制定一个可执行的步骤计划：{state['task']}")
        return {"plan": plan.content}

    def execute(state: ReflexionState) -> dict:
        """按当前计划执行任务。"""
        return executor(state)

    def reflect(state: ReflexionState) -> dict:
        """失败后生成文字反思，追加到历史反思列表。"""
        reflection = reflector(state)
        return {"reflections": [*state.get("reflections", []), reflection]}

    def replan(state: ReflexionState) -> dict:
        """结合历史反思重新规划，并把本次反思一并注入提示词。"""
        reflections = "\n".join(f"- {item}" for item in state.get("reflections", []))
        prompt = (
            "之前的计划失败了。\n"
            f"任务：{state['task']}\n"
            f"失败原因：{state['error']}\n"
            f"历史反思：\n{reflections}\n"
            "请结合上述反思制定新的计划，避免重复之前的错误。"
        )
        new_plan = llm.invoke(prompt)
        return {"plan": new_plan.content, "retry_count": state["retry_count"] + 1}

    builder = StateGraph(ReflexionState)
    builder.add_node("plan", plan_task)
    builder.add_node("execute", execute)
    builder.add_node("reflect", reflect)
    builder.add_node("replan", replan)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    # 复用 Replanning 的判定规则：失败且未超上限才进入反思环节。
    builder.add_conditional_edges("execute", should_replan, {"replan": "reflect", "end": END})
    builder.add_edge("reflect", "replan")
    builder.add_edge("replan", "execute")
    return builder.compile()


def run_task_with_reflexion(
    task: str,
    *,
    executor: Executor | None = None,
    llm: Any | None = None,
    reflector: Reflector | None = None,
) -> ReflexionState:
    """执行任务：失败后先反思再重规划，最多重试 MAX_RETRIES 次，返回最终状态。"""
    graph = build_reflexion_graph(executor=executor, llm=llm, reflector=reflector)
    initial: ReflexionState = {
        "task": task,
        "plan": "",
        "status": "pending",
        "result": "",
        "error": "",
        "retry_count": 0,
        "reflections": [],
    }
    result: ReflexionState = graph.invoke(initial)
    return result
