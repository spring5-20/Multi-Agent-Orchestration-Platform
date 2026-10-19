from ...config import settings
from .bi_adapter import BISubAgent
from .dingtalk_adapter import DingTalkSubAgent
from .ecommerce_adapter import ECommerceSubAgent
from .engine import register_agent
from .qweather_adapter import QWeatherSubAgent

_INPROCESS_AGENT_CLASSES = (QWeatherSubAgent, DingTalkSubAgent, ECommerceSubAgent, BISubAgent)


def _register_defaults() -> None:
    """按配置注册进程内实现或 A2A 远程客户端。"""
    if settings.ORCHESTRATOR_SUBAGENT_TRANSPORT == "a2a":
        from ..a2a.client import A2ASubAgent

        base = settings.A2A_PUBLIC_BASE_URL.rstrip("/")
        for cls in _INPROCESS_AGENT_CLASSES:
            register_agent(
                A2ASubAgent(
                    cls.name,
                    cls.description,
                    f"{base}/a2a/{cls.name}",
                    confirmation_required=cls.confirmation_required,
                )
            )
    else:
        for cls in _INPROCESS_AGENT_CLASSES:
            register_agent(cls())


_register_defaults()
