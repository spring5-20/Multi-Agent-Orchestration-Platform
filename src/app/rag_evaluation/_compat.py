"""兼容性补丁：修复 ragas 0.4.x 与 langchain-community 0.4.x 的导入冲突。

``langchain-community`` 0.4.2 已移除 ``chat_models.vertexai`` 模块（官方 sunset 后
相关集成迁移到独立包），但 ragas 0.4.3 的 ``ragas.llms.base`` 仍硬编码执行
``from langchain_community.chat_models.vertexai import ChatVertexAI``，
该符号仅用于 ``isinstance`` 类型判断，不会被真正调用。

这里在导入 ragas 之前注入一个最小占位模块，使 ragas 可以正常导入，
不影响本项目实际使用的 DeepSeek（OpenAI 兼容）裁判模型。
"""

from __future__ import annotations

import sys
import types

_MODULE_NAME = "langchain_community.chat_models.vertexai"


class _ChatVertexAIStub:
    """``ChatVertexAI`` 的占位实现，仅满足 ragas 的 ``isinstance`` 判断。"""


def install_compat_stub() -> None:
    """在导入 ragas 前调用，注入缺失的 vertexai 占位模块。"""
    if _MODULE_NAME in sys.modules:
        return

    # 先导入父包，避免父包 __init__ 后续覆盖子模块注册。
    import langchain_community.chat_models  # noqa: F401

    stub = types.ModuleType(_MODULE_NAME)
    stub.ChatVertexAI = _ChatVertexAIStub  # type: ignore[attr-defined]
    sys.modules[_MODULE_NAME] = stub
