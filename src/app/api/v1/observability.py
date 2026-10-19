"""可观测性 API：Trace / Metric / Log 三层的查询与导出。

- GET /observability/status：当前配置（LangSmith 是否真正开启、指标窗口）；
- GET /observability/metrics：指标快照（JSON，延迟 / 错误率 / Token / 成本）；
- GET /observability/prometheus：Prometheus 文本格式，供 Prometheus + Grafana 抓取；
- GET /observability/traces：最近若干轮本地分层轨迹（Trace 层的离线兜底）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from ...core.config import settings
from ...core.observability import langsmith_enabled, metrics_snapshot, recent_traces, render_prometheus

router = APIRouter(prefix="/observability", tags=["可观测性"])


@router.get("/status", summary="可观测性配置状态")
async def observability_status() -> dict[str, Any]:
    """返回三层可观测性的当前配置，便于前端与排障时确认是否启用。"""
    return {
        "langsmith_enabled": langsmith_enabled(),
        "langsmith_project": settings.LANGSMITH_PROJECT,
        "trace_window": settings.OBSERVABILITY_TRACE_WINDOW,
        "structured_log": settings.OBSERVABILITY_STRUCTURED_LOG,
        "cost_per_1k_input": settings.OBSERVABILITY_COST_PER_1K_INPUT,
        "cost_per_1k_output": settings.OBSERVABILITY_COST_PER_1K_OUTPUT,
    }


@router.get("/metrics", summary="指标快照（JSON）")
async def observability_metrics() -> dict[str, Any]:
    """返回进程内指标快照：错误率、延迟分位数、Token 与估算成本。"""
    return metrics_snapshot()


@router.get("/prometheus", response_class=PlainTextResponse, summary="Prometheus 文本指标")
async def observability_prometheus() -> str:
    """按 Prometheus 文本协议导出指标，可直接配置为抓取目标。"""
    return render_prometheus()


@router.get("/traces", summary="最近若干轮本地轨迹")
async def observability_traces(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """返回最近若干轮的本地分层轨迹（新的在前），用于离线查看多 Agent 协作链路。"""
    return {"traces": recent_traces(limit)}
