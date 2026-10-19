"""从对话中提取值得长期记忆的用户信息。

调用大模型输出结构化 JSON，解析为偏好 / 事实 / 事件三类记忆，
并顺带抽取用户画像字段（姓名、预算、品类等自由键值）。
"""

import json
import logging
import re
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from ...config import settings
from .memory.base import CLEAR

logger = logging.getLogger(__name__)
_TRIVIAL_PATTERN = re.compile(
    r"^(你好|您好|早上好|中午好|晚上好|晚安|hi|hello|在吗|谢谢|多谢|再见|拜拜|好的|ok|嗯)[！!。.？?~～\s]*$",
    re.IGNORECASE,
)


def worth_extracting(user_message: str) -> bool:
    """粗筛明显无记忆价值的对话，避免对纯寒暄发起 LLM 提取调用。

    仅做低成本启发式判断（长度 + 纯问候语），真正的「是否有可记忆信息」
    仍由提取模型输出决定；本函数返回 False 时直接跳过，节省一次大模型调用。
    """
    text = user_message.strip()
    if len(text) < 2:
        return False
    return _TRIVIAL_PATTERN.match(text) is None


_EXTRACT_PROMPT = """你是记忆提取器，从对话中提取值得长期记住的用户信息。

用户：{user_message}
助手：{assistant_message}

请只输出 JSON，不要输出任何 JSON 之外的内容。格式：
{{
  "preferences": ["除品类/预算外的其他偏好，如品牌、颜色、风格"],
  "facts": ["无法结构化的陈述性事实，如职业、身份、用途"],
  "events": ["发生的事件，如已购买/已退货"],
  "profile": {{"name": null, "category": null, "budget": null}}
}}

提取规则：
1. category 填用户主营或感兴趣的品类（如"运动鞋"、"数码"、"服饰"），只要对话提到具体品类就填。
2. budget 填用户明确的预算上限数字（例如只说"500"就填 500，不要带单位或"以内"）。
3. name 填用户自我介绍的名字，没有就填 null。
4. 能被 profile 结构化的信息，不要重复放进 preferences 或 facts，避免冗余。
5. 用户明确否定或取消此前信息时（如"我没有预算限制""不用管品类了"），对应 profile 字段填 "__clear__"。
6. 没有对应信息时填 null 或空数组。

示例：
用户说"我是做电商的，主要卖运动鞋，预算控制在500以内"，应输出：
{{
  "preferences": [],
  "facts": ["用户是做电商的"],
  "events": [],
  "profile": {{"name": null, "category": "运动鞋", "budget": 500}}
}}"""


def _build_llm() -> ChatOpenAI:
    """创建用于记忆提取的大模型客户端（非流式）。"""
    if not settings.DEEPSEEK_API_KEY or not settings.DEEPSEEK_MODEL or not settings.DEEPSEEK_BASE_URL:
        raise RuntimeError("DeepSeek 配置不完整，请检查 DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL")
    return ChatOpenAI(
        model=settings.DEEPSEEK_MODEL,
        api_key=SecretStr(settings.DEEPSEEK_API_KEY),
        base_url=settings.DEEPSEEK_BASE_URL,
        temperature=0,
        timeout=60,
    )


_llm: ChatOpenAI | None = None


def _get_llm() -> ChatOpenAI:
    """获取记忆提取大模型客户端单例（惰性创建，复用连接配置）。"""
    global _llm
    if _llm is None:
        _llm = _build_llm()
    return _llm


def _normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """规整画像字段：保留删除哨兵，剔除空值，budget 字符串数字转数值。"""
    normalized: dict[str, Any] = {}
    for key, value in profile.items():
        if value == CLEAR:
            normalized[key] = CLEAR
            continue
        if value in (None, "", [], {}):
            continue
        if key == "budget" and isinstance(value, str):
            try:
                number = float(value)
                value = int(number) if number.is_integer() else number
            except ValueError:
                continue
        normalized[key] = value
    return normalized


def _to_text(content: Any) -> str:
    """把模型返回内容规整为字符串（兼容 content 为字符串或分块列表）。"""
    if isinstance(content, list):
        return "".join(str(part) for part in content)
    return str(content or "")


def _strip_code_fence(text: str) -> str:
    """去除可能包裹 JSON 的 markdown 代码块标记。"""
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()


async def extract_memory_items(user_message: str, assistant_message: str) -> dict[str, Any]:
    """提取记忆项与画像字段，返回 ``preferences`` / ``facts`` / ``events`` / ``profile``。"""
    llm = _get_llm()
    prompt = _EXTRACT_PROMPT.format(user_message=user_message, assistant_message=assistant_message)
    response = await llm.ainvoke(prompt)
    text = _strip_code_fence(_to_text(response.content))

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("记忆提取 JSON 解析失败，返回空结果: %s", text[:200])
        return {"preferences": [], "facts": [], "events": [], "profile": {}}

    return {
        "preferences": [str(item) for item in (data.get("preferences") or [])],
        "facts": [str(item) for item in (data.get("facts") or [])],
        "events": [str(item) for item in (data.get("events") or [])],
        "profile": _normalize_profile(data.get("profile") or {}),
    }
