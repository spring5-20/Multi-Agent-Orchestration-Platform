"""内置黄金测试集：覆盖天气 / 电商 / 钉钉三条主链路，回归测试开箱即用。"""

from __future__ import annotations

from .schemas import AgentEvalCase

#: 三条主链路的代表性用例；expected_agents 用于对照工具调用准确率。
DEFAULT_GOLDEN_CASES: tuple[AgentEvalCase, ...] = (
    AgentEvalCase(
        question="北京今天适合跑步吗？",
        expected_agents=["qweather"],
        expected_keywords=["北京"],
    ),
    AgentEvalCase(
        question="推荐 500 元以内的跑鞋",
        expected_agents=["ecommerce"],
    ),
    AgentEvalCase(
        question="给张三发条消息：会议改到下午三点",
        expected_agents=["dingtalk"],
    ),
)
