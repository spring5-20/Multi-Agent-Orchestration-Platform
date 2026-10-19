"""RAG 评估模块：基于 RAGAS 对 RAG 系统的检索与生成质量打分。

本模块与现有 RAG 流程解耦，只依赖 ``RagRunner`` 协议获取问答产物，
不侵入 ``api/v1`` 下的 RAG 实现。
"""

from ._compat import install_compat_stub

# 必须在导入 ragas 前安装补丁，否则 ragas 会因缺失 vertexai 模块而失败。
install_compat_stub()

from .service import EvaluationService  # noqa: E402

__all__ = ["EvaluationService"]
