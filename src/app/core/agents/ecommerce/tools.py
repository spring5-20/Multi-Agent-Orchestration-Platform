"""电商导购智能体的工具：商品检索。"""

import json
import logging

from langchain_core.tools import tool

from ...config import settings
from .runtime import get_product_source

logger = logging.getLogger(__name__)


@tool
async def search_products(
    query: str,
    category: str | None = None,
    budget_max: float | None = None,
) -> str:
    """在商品库中检索商品，用于回答用户的商品推荐、选品或购物问题。

    Args:
        query: 检索关键词或用户的商品需求描述。
        category: 品类过滤（如"运动鞋"），用户没有明确品类时传 None。
        budget_max: 预算上限（元），用户没有预算要求时传 None。

    Returns:
        商品列表的 JSON 字符串；没有匹配时返回提示文本。
    """
    source = get_product_source()
    products = await source.search(
        query,
        category=category,
        budget_max=budget_max,
        top_k=settings.ECOMMERCE_PRODUCT_TOP_K,
    )
    if not products:
        return "没有找到符合要求的商品。"
    return json.dumps([product.to_dict() for product in products], ensure_ascii=False)
