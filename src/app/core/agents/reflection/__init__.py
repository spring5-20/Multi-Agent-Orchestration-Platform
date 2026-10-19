"""规划与反思智能体：Reflection、Replanning 与 Reflexion。

- Reflection：生成 → 批评 → 修订（单轮自我改进）；
- Replanning：失败后重新规划，换条路走；
- Reflexion：失败后生成文字反思并注入后续尝试，避免重复犯错。
"""

from .graph import (
    MAX_RETRIES,
    ReflectionState,
    TaskState,
    build_reflection_graph,
    build_replanning_graph,
    run_reflection,
    run_task_with_replanning,
    should_replan,
)
from .reflexion import (
    Reflector,
    ReflexionState,
    build_reflexion_graph,
    run_task_with_reflexion,
)

__all__ = [
    "MAX_RETRIES",
    "ReflectionState",
    "ReflexionState",
    "Reflector",
    "TaskState",
    "build_reflection_graph",
    "build_reflexion_graph",
    "build_replanning_graph",
    "run_reflection",
    "run_task_with_reflexion",
    "run_task_with_replanning",
    "should_replan",
]
