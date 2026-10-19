"""可观测性：Trace（LangSmith + 本地）、Metric（指标）、Log（结构化日志）三层。

多 Agent 系统比单 Agent 更复杂，需要追踪每个 Agent 的决策、工具调用、状态变化。
本模块把课件里的三个层次落到实处：

1. Trace：启用 LangSmith 后，LangChain / LangGraph 自动上报分层追踪
   （原生支持节点和边语义、Supervisor-Worker 模式）；同时用进程内的
   TraceContext 记录最近若干轮轨迹，作为离线可查、可测试的兜底。
2. Metric：MetricsCollector 采集延迟、错误率、Token 消耗与估算成本，
   并可导出 Prometheus 文本格式（对接 Prometheus + Grafana）。
3. Log：log_event 输出 JSON 结构化日志，每个节点的关键动作都有字段可查。

应用启动时由 configure_observability 与 apply_langsmith_settings
写入配置，业务代码只需调用 start_trace / log_event。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------- Trace：LangSmith 环境变量 ----------------


class _LangSmithConfig(Protocol):
    """本模块需要的 LangSmith 配置字段（与 config.Settings 结构一致）。"""

    LANGSMITH_TRACING: bool
    LANGSMITH_API_KEY: str | None
    LANGSMITH_PROJECT: str | None
    LANGSMITH_ENDPOINT: str | None


def apply_langsmith_settings(settings: _LangSmithConfig) -> None:
    """把 LangSmith 配置写入进程环境变量。

    仅当 LANGSMITH_TRACING 打开且配置了 API Key 时才真正启用，
    否则显式关闭，避免环境中残留的变量意外开启上报。
    """
    enabled = settings.LANGSMITH_TRACING and bool(settings.LANGSMITH_API_KEY)
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    if settings.LANGSMITH_API_KEY:
        os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY
    if settings.LANGSMITH_PROJECT:
        os.environ["LANGSMITH_PROJECT"] = settings.LANGSMITH_PROJECT
    if settings.LANGSMITH_ENDPOINT:
        os.environ["LANGSMITH_ENDPOINT"] = settings.LANGSMITH_ENDPOINT

    if enabled:
        logger.info("已启用 LangSmith 追踪，project=%s", settings.LANGSMITH_PROJECT)
    else:
        logger.debug("LangSmith 追踪未启用（LANGSMITH_TRACING=%s）", settings.LANGSMITH_TRACING)


def langsmith_enabled() -> bool:
    """当前进程是否真正开启了 LangSmith 上报（供状态接口展示）。"""
    return os.environ.get("LANGSMITH_TRACING", "").lower() == "true" and bool(os.environ.get("LANGSMITH_API_KEY"))


# ---------------- Metric / Log 层配置 ----------------


class _ObservabilityConfig(Protocol):
    """本模块需要的 Metric / Log 相关配置字段。"""

    OBSERVABILITY_TRACE_WINDOW: int
    OBSERVABILITY_COST_PER_1K_INPUT: float
    OBSERVABILITY_COST_PER_1K_OUTPUT: float
    OBSERVABILITY_STRUCTURED_LOG: bool


@dataclass
class _ObservabilityRuntime:
    """进程内可观测性运行时参数（由 configure_observability 更新）。"""

    trace_window: int = 200
    cost_per_1k_input: float = 0.002
    cost_per_1k_output: float = 0.008
    structured_log: bool = True


_runtime = _ObservabilityRuntime()


def configure_observability(settings: _ObservabilityConfig) -> None:
    """把 Metric / Log 层配置写入运行时，并调整本地轨迹保留窗口。"""
    _runtime.trace_window = max(1, int(settings.OBSERVABILITY_TRACE_WINDOW))
    _runtime.cost_per_1k_input = float(settings.OBSERVABILITY_COST_PER_1K_INPUT)
    _runtime.cost_per_1k_output = float(settings.OBSERVABILITY_COST_PER_1K_OUTPUT)
    _runtime.structured_log = bool(settings.OBSERVABILITY_STRUCTURED_LOG)
    collector.set_window(_runtime.trace_window)
    global _traces
    with _traces_lock:
        _traces = deque(_traces, maxlen=_runtime.trace_window)


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """输出一条结构化日志（Log 层）。

    开启结构化日志时输出单行 JSON，便于日志系统按字段检索；关闭时退化为
    logger.log 的普通文本，保持向后兼容。
    """
    if _runtime.structured_log:
        payload = {"event": event, "ts": round(time.time(), 3), **fields}
        logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))
    else:
        logger.log(level, "%s %s", event, fields or "")


# ---------------- Trace：本地分层轨迹 ----------------


@dataclass
class Span:
    """一个执行跨度：一次节点 / 子智能体调用。"""

    name: str
    node: str
    started_at: float
    ended_at: float | None = None
    status: str = "running"
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        """跨度耗时（秒）；未结束时按当前时间估算。"""
        end = self.ended_at if self.ended_at is not None else time.time()
        return round(end - self.started_at, 4)

    def finish(self, *, status: str, **attributes: Any) -> None:
        """结束跨度并写入状态与属性。"""
        self.ended_at = time.time()
        self.status = status
        self.attributes.update(attributes)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可返回给前端 / 落库的字典。"""
        return {
            "name": self.name,
            "node": self.node,
            "status": self.status,
            "duration": self.duration,
            "attributes": self.attributes,
        }


@dataclass
class Trace:
    """一轮完整对话的分层轨迹（Trace 层）。"""

    trace_id: str
    question: str
    started_at: float
    spans: list[Span] = field(default_factory=list)
    ended_at: float | None = None
    status: str = "running"
    answer: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def duration(self) -> float:
        """端到端耗时（秒）。"""
        end = self.ended_at if self.ended_at is not None else time.time()
        return round(end - self.started_at, 4)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可返回给前端的字典。"""
        return {
            "trace_id": self.trace_id,
            "question": self.question,
            "status": self.status,
            "duration": self.duration,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "answer": self.answer,
            "spans": [span.to_dict() for span in self.spans],
        }


class TraceContext:
    """一轮对话的本地轨迹上下文：记录子智能体跨度、Token 与最终结果。"""

    def __init__(self, question: str) -> None:
        self.trace = Trace(trace_id=uuid.uuid4().hex, question=question, started_at=time.time())
        self._running: dict[str, list[Span]] = {}

    @property
    def trace_id(self) -> str:
        return self.trace.trace_id

    def span(self, name: str, node: str = "", **attributes: Any) -> Span:
        """开启一个跨度（例如 supervisor 决策或某个子智能体调用）。"""
        span = Span(name=name, node=node, started_at=time.time(), attributes=dict(attributes))
        self.trace.spans.append(span)
        self._running.setdefault(name, []).append(span)
        return span

    def close_span(self, name: str, *, status: str, **attributes: Any) -> None:
        """结束最近一个同名跨度（先入先出，支撑同名子智能体被多次派单）。"""
        pending = self._running.get(name)
        if not pending:
            return
        pending.pop(0).finish(status=status, **attributes)

    def add_tokens(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """累加本轮 LLM 的 Token 用量（来自 LangChain 的 usage_metadata）。"""
        self.trace.input_tokens += max(0, int(input_tokens))
        self.trace.output_tokens += max(0, int(output_tokens))

    def finish(self, *, answer: str, ok: bool) -> None:
        """结束本轮轨迹：写入最终回答与状态，并汇入指标采集器。"""
        self.trace.ended_at = time.time()
        self.trace.status = "success" if ok else "failed"
        self.trace.answer = answer
        # 未正常关闭的跨度统一标记为失败，避免轨迹里出现悬挂的 running。
        for pending in self._running.values():
            for span in pending:
                span.finish(status="failed", note="未收到结束事件")
        self._running.clear()
        collector.record(self.trace)


# ---------------- Metric：指标采集 ----------------


class MetricsCollector:
    """进程内指标采集器：错误率、延迟、Token 与估算成本。

    只保留最近 window 轮的明细用于分位数统计，累计计数器则从进程启动开始累加。
    导出格式与 Prometheus 文本协议兼容，可被 Prometheus 抓取后在 Grafana 展示。
    """

    def __init__(self, window: int = 200) -> None:
        self._lock = Lock()
        self._window: deque[dict[str, Any]] = deque(maxlen=window)
        self._turns_total = 0
        self._turns_failed_total = 0
        self._agent_calls: dict[str, int] = {}
        self._agent_failures: dict[str, int] = {}
        self._input_tokens_total = 0
        self._output_tokens_total = 0

    def set_window(self, window: int) -> None:
        """调整明细窗口大小（保留已有明细中最近的若干条）。"""
        with self._lock:
            if window == self._window.maxlen:
                return
            self._window = deque(self._window, maxlen=window)

    def record(self, trace: Trace) -> None:
        """记录一轮轨迹：累计计数并更新明细窗口。"""
        agent_calls = [span.name for span in trace.spans if span.node == "agent"]
        agent_failures = [span.name for span in trace.spans if span.node == "agent" and span.status == "failed"]
        with self._lock:
            self._turns_total += 1
            if trace.status != "success":
                self._turns_failed_total += 1
            for name in agent_calls:
                self._agent_calls[name] = self._agent_calls.get(name, 0) + 1
            for name in agent_failures:
                self._agent_failures[name] = self._agent_failures.get(name, 0) + 1
            self._input_tokens_total += trace.input_tokens
            self._output_tokens_total += trace.output_tokens
            self._window.append(
                {
                    "trace_id": trace.trace_id,
                    "status": trace.status,
                    "duration": trace.duration,
                    "agent_calls": agent_calls,
                    "agent_failures": agent_failures,
                    "input_tokens": trace.input_tokens,
                    "output_tokens": trace.output_tokens,
                }
            )

    def reset(self) -> None:
        """清空全部指标（测试用）。"""
        with self._lock:
            self._window.clear()
            self._turns_total = 0
            self._turns_failed_total = 0
            self._agent_calls.clear()
            self._agent_failures.clear()
            self._input_tokens_total = 0
            self._output_tokens_total = 0

    def snapshot(self) -> dict[str, Any]:
        """返回指标快照（JSON 友好，供前端与接口使用）。"""
        with self._lock:
            window = list(self._window)
            turns_total = self._turns_total
            turns_failed_total = self._turns_failed_total
            agent_calls = dict(self._agent_calls)
            agent_failures = dict(self._agent_failures)
            input_tokens_total = self._input_tokens_total
            output_tokens_total = self._output_tokens_total

        durations = sorted(item["duration"] for item in window)
        window_failed = sum(1 for item in window if item["status"] != "success")
        cost_total = _estimate_cost(input_tokens_total, output_tokens_total)
        cost_window = sum(_estimate_cost(item["input_tokens"], item["output_tokens"]) for item in window)

        return {
            "turns_total": turns_total,
            "turns_failed_total": turns_failed_total,
            "error_rate": round(turns_failed_total / turns_total, 4) if turns_total else 0.0,
            "window_size": len(window),
            "window_error_rate": round(window_failed / len(window), 4) if window else 0.0,
            "avg_latency": round(sum(durations) / len(durations), 4) if durations else 0.0,
            "p50_latency": _quantile(durations, 0.5),
            "p95_latency": _quantile(durations, 0.95),
            "agent_calls": agent_calls,
            "agent_failures": agent_failures,
            "input_tokens_total": input_tokens_total,
            "output_tokens_total": output_tokens_total,
            "cost_total": round(cost_total, 6),
            "avg_cost_per_turn": round(cost_window / len(window), 6) if window else 0.0,
            "cost_unit": "CNY（按配置单价估算）",
        }

    def render_prometheus(self) -> str:
        """按 Prometheus 文本协议导出指标。"""
        snap = self.snapshot()
        lines = [
            "# HELP agent_turns_total 累计对话轮次",
            "# TYPE agent_turns_total counter",
            f"agent_turns_total {snap['turns_total']}",
            "# HELP agent_turns_failed_total 累计失败轮次",
            "# TYPE agent_turns_failed_total counter",
            f"agent_turns_failed_total {snap['turns_failed_total']}",
            "# HELP agent_turn_latency_seconds 单轮端到端耗时（窗口分位数）",
            "# TYPE agent_turn_latency_seconds gauge",
            f'agent_turn_latency_seconds{{quantile="0.5"}} {snap["p50_latency"]}',
            f'agent_turn_latency_seconds{{quantile="0.95"}} {snap["p95_latency"]}',
            "# HELP agent_tokens_total LLM Token 用量",
            "# TYPE agent_tokens_total counter",
            f'agent_tokens_total{{kind="input"}} {snap["input_tokens_total"]}',
            f'agent_tokens_total{{kind="output"}} {snap["output_tokens_total"]}',
            "# HELP agent_cost_cny_total 估算成本（元）",
            "# TYPE agent_cost_cny_total counter",
            f"agent_cost_cny_total {snap['cost_total']}",
        ]
        for name, count in sorted(snap["agent_calls"].items()):
            lines.append(f'agent_calls_total{{agent="{name}"}} {count}')
        for name, count in sorted(snap["agent_failures"].items()):
            lines.append(f'agent_failures_total{{agent="{name}"}} {count}')
        return "\n".join(lines) + "\n"


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """按配置单价估算成本（元）。"""
    return input_tokens / 1000 * _runtime.cost_per_1k_input + output_tokens / 1000 * _runtime.cost_per_1k_output


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """按当前配置单价估算成本（元），供评估模块与其他调用方复用。"""
    return round(_estimate_cost(input_tokens, output_tokens), 6)


def _quantile(sorted_values: list[float], q: float) -> float:
    """在已排序列表上取分位数（最近邻法，足够指标展示用）。"""
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return round(sorted_values[index], 4)


# ---------------- 进程内单例：指标采集器与最近轨迹环形缓冲 ----------------

collector = MetricsCollector()
_traces: deque[Trace] = deque(maxlen=_runtime.trace_window)
_traces_lock = Lock()


def start_trace(question: str) -> TraceContext:
    """开启一轮对话的本地轨迹记录，返回可操作的上下文。"""
    return TraceContext(question)


def finish_trace(context: TraceContext, *, answer: str, ok: bool) -> Trace:
    """结束一轮轨迹并写入最近轨迹缓冲。"""
    context.finish(answer=answer, ok=ok)
    with _traces_lock:
        _traces.append(context.trace)
    return context.trace


def recent_traces(limit: int = 20) -> list[dict[str, Any]]:
    """返回最近若干轮轨迹（新的在前）。"""
    with _traces_lock:
        items = list(_traces)[-max(1, limit) :]
    return [trace.to_dict() for trace in reversed(items)]


def metrics_snapshot() -> dict[str, Any]:
    """返回当前指标快照。"""
    return collector.snapshot()


def render_prometheus() -> str:
    """返回 Prometheus 文本格式指标。"""
    return collector.render_prometheus()


def reset_observability() -> None:
    """清空指标与轨迹（测试用）。"""
    collector.reset()
    with _traces_lock:
        _traces.clear()
