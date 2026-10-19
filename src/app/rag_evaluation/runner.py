"""RAG 运行器抽象：评估模块只依赖该接口，不侵入现有 RAG 流程。

通过 ``RagRunner`` 协议解耦：生产环境用 ``LangGraphRagRunner`` 调用现有
LangGraph 图，测试用 ``MockRagRunner`` 返回固定产物，不依赖数据库与外部模型。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .schemas import EvalTrace


@runtime_checkable
class RagRunner(Protocol):
    """RAG 运行器协议：输入问题与参数，产出回答与检索上下文。"""

    async def run(self, question: str, **params: Any) -> EvalTrace: ...


class LangGraphRagRunner:
    """调用现有 LangGraph 在线 RAG 图，产出答案与上下文。"""

    def __init__(self, db: Any = None) -> None:
        self._db = db
        self._graph: Any = None

    def _get_graph(self) -> Any:
        """惰性构建并缓存 LangGraph 图。"""
        if self._graph is None:
            from app.api.v1.langgraph_ask import build_rag_graph

            self._graph = build_rag_graph(self._db)
        return self._graph

    async def run(self, question: str, **params: Any) -> EvalTrace:
        """执行一次完整 RAG 问答，返回回答与检索上下文。"""
        graph = self._get_graph()

        initial_state: dict[str, Any] = {
            "question": question,
            # source_type 使用空串而非 None，避免 LangGraph 通道恢复时丢弃。
            "source_type": params.get("source_type", ""),
            "top_k": params.get("top_k", 20),
            "rerank_top_n": params.get("rerank_top_n", 3),
            # max_attempts=0 使生成后直接结束，评估场景不触发人工审批中断。
            "max_attempts": 0,
            "attempt": 0,
            "documents": [],
            "sources": [],
            "answer": "",
            "rewrite_approved": False,
        }

        result = await graph.ainvoke(initial_state)
        answer = str(result.get("answer", "") or "")
        sources: list[dict[str, Any]] = list(result.get("sources") or [])
        contexts = [str(source.get("content") or "") for source in sources if source.get("content")]

        return EvalTrace(question=question, answer=answer, contexts=contexts, sources=sources)


class MockRagRunner:
    """测试用运行器：按预置映射返回固定产物，不依赖外部依赖。"""

    def __init__(self, traces: dict[str, EvalTrace] | None = None) -> None:
        self._traces = traces or {}

    async def run(self, question: str, **params: Any) -> EvalTrace:
        """优先返回预置产物；未预置时按问题生成固定的模拟回答与上下文。"""
        trace = self._traces.get(question)
        if trace is not None:
            return trace
        return EvalTrace(
            question=question,
            answer=f"这是对「{question}」的模拟回答。",
            contexts=[f"关于「{question}」的模拟上下文。"],
        )
