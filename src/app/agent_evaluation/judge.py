"""LLM-as-Judge：用大模型评估大模型的答案质量（切题、正确、无编造）。

裁判输出一行 JSON：{"score": 0~1 的小数, "reason": "一句话理由"}。
解析失败或调用失败时返回 score=None，只丢这一项评分，不影响规则类指标。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Protocol, runtime_checkable

from app.core.config import settings

from .schemas import JudgeScore

_JUDGE_PROMPT = """你是严格的评测裁判。请判断【模型回答】是否切题、正确、无编造。

问题：{question}
标准答案：{ground_truth}
模型回答：{answer}

只输出一行 JSON，不要额外解释：{{"score": 0~1 的小数, "reason": "一句话理由"}}
"""


@runtime_checkable
class Judge(Protocol):
    """裁判协议：任何实现 score 的对象都能注入评估服务（便于测试）。"""

    async def score(self, *, question: str, answer: str, ground_truth: str | None = None) -> JudgeScore: ...


class LLMJudge:
    """基于 DeepSeek（OpenAI 兼容接口）的裁判实现。"""

    def __init__(self, llm: Any | None = None, model: str | None = None) -> None:
        self._llm = llm
        self._model = model or settings.AGENT_JUDGE_MODEL or "deepseek-chat"

    def _get_llm(self) -> Any:
        """惰性创建裁判模型（temperature=0，尽量稳定）。"""
        if self._llm is None:
            from langchain_openai import ChatOpenAI
            from pydantic import SecretStr

            self._llm = ChatOpenAI(
                model=self._model,
                api_key=SecretStr(settings.DEEPSEEK_API_KEY or ""),
                base_url=settings.DEEPSEEK_BASE_URL,
                temperature=0,
                timeout=60,
            )
        return self._llm

    async def score(self, *, question: str, answer: str, ground_truth: str | None = None) -> JudgeScore:
        """给一条回答打分；任何异常都降级为 score=None 并附带原因。"""
        prompt = _JUDGE_PROMPT.format(
            question=question,
            ground_truth=ground_truth or "（未提供）",
            answer=answer or "（空回答）",
        )
        try:
            response = await asyncio.to_thread(self._get_llm().invoke, prompt)
            payload = _parse_json(str(getattr(response, "content", response)))
            raw = payload.get("score")
            score = float(raw) if raw is not None else None
            if score is not None:
                score = min(1.0, max(0.0, score))
            return JudgeScore(score=score, reason=str(payload.get("reason") or ""), model=self._model)
        except Exception as exc:  # noqa: BLE001 - 判官失败不应中断整批评估。
            return JudgeScore(
                score=None,
                reason=f"裁判调用失败：{type(exc).__name__}: {exc}",
                model=self._model,
            )


def _parse_json(text: str) -> dict[str, Any]:
    """从模型输出里尽力解析出 JSON；失败时退化为抓取一个 0~1 的数字。"""
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    number = re.search(r"(0(?:\.\d+)?|1(?:\.0+)?)", text)
    return {"score": float(number.group(0)) if number else None, "reason": text.strip()[:200]}
