"""内置商品目录：零外部依赖的结构化商品库。

用于开发环境与 ``hybrid`` 模式的兜底，关键词 + 品类 + 价格区间过滤。
后续可替换为真实商品库接口，无需改动调用方。
"""

import re

from .base import Product, ProductSource

# 内置商品清单（演示用）：字段为 (id, name, category, description, price)
_CATALOG: list[tuple[str, str, str, str, float]] = [
    ("p001", "轻便透气跑步鞋", "运动鞋", "适合日常慢跑，网面透气，缓震鞋底", 299.0),
    ("p002", "专业缓震跑步鞋", "运动鞋", "马拉松训练适用，全掌缓震，支撑稳定", 459.0),
    ("p003", "复古百搭小白鞋", "运动鞋", "经典板鞋造型，日常穿搭百搭", 249.0),
    ("p004", "篮球实战球鞋", "运动鞋", "高帮护踝设计，抓地耐磨外底", 529.0),
    ("p005", "竞速碳板跑鞋", "运动鞋", "全掌碳板推进，轻量竞速", 899.0),
    ("p006", "户外登山鞋", "运动鞋", "防水防滑，适合徒步登山", 399.0),
    ("p007", "运动速干T恤", "服饰", "吸湿排汗，透气速干", 79.0),
    ("p008", "跑步压缩裤", "服饰", "高弹面料，提供肌肉支撑", 129.0),
    ("p009", "防风运动外套", "服饰", "轻量防风，可折叠收纳", 199.0),
    ("p010", "运动休闲卫衣", "服饰", "加绒保暖，宽松版型", 169.0),
    ("p011", "无线蓝牙耳机", "数码", "主动降噪，续航 30 小时", 299.0),
    ("p012", "骨传导运动耳机", "数码", "不入耳设计，跑步更安全", 399.0),
    ("p013", "智能运动手表", "数码", "心率血氧监测，GPS 定位", 699.0),
    ("p014", "运动水杯", "配件", "大容量，吸管设计", 49.0),
    ("p015", "瑜伽垫", "配件", "加厚防滑，附收纳绑带", 89.0),
    ("p016", "运动双肩包", "配件", "独立鞋仓，防泼水", 159.0),
    ("p017", "标准七号篮球", "配件", "室内外通用，耐磨皮革", 119.0),
    ("p018", "竞速跳绳", "配件", "轴承顺滑，长度可调", 29.0),
    ("p019", "护膝护具", "配件", "运动防护，透气加压", 59.0),
    ("p020", "深层放松筋膜枪", "配件", "多档力度，缓解肌肉疲劳", 249.0),
]


def _tokenize(text: str) -> set[str]:
    """简化分词：抽取连续中文单字、英文单词与数字。"""
    return set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", text.lower()))


class BuiltinProductSource(ProductSource):
    """基于内置商品清单的关键词检索。"""

    def __init__(self, catalog: list[tuple[str, str, str, str, float]] | None = None) -> None:
        self._catalog = catalog or _CATALOG

    def _build_candidates(
        self,
        query_tokens: set[str],
        *,
        category: str | None,
        budget_min: float | None,
        budget_max: float | None,
        require_overlap: bool,
    ) -> list[tuple[float, Product]]:
        """按品类与价格过滤目录，并计算关键词相关得分。"""
        candidates: list[tuple[float, Product]] = []
        for product_id, name, cat, desc, price in self._catalog:
            if category and category not in cat and cat not in category:
                continue
            if budget_min is not None and price < budget_min:
                continue
            if budget_max is not None and price > budget_max:
                continue

            text_tokens = _tokenize(f"{name} {cat} {desc}")
            overlap = len(query_tokens & text_tokens)
            if require_overlap and query_tokens and overlap == 0:
                continue
            score = overlap / max(len(query_tokens), 1) if query_tokens else 0.0
            candidates.append((score, Product(product_id, name, cat, desc, price, score=round(score, 4))))
        return candidates

    async def search(
        self,
        query: str,
        *,
        category: str | None = None,
        budget_min: float | None = None,
        budget_max: float | None = None,
        top_k: int = 5,
    ) -> list[Product]:
        query_tokens = _tokenize(query)
        scored = self._build_candidates(
            query_tokens,
            category=category,
            budget_min=budget_min,
            budget_max=budget_max,
            require_overlap=True,
        )
        # 兜底 1：用户说的品类词与目录口径不一致时（如用户说「跑鞋」、目录记「运动鞋」），
        # 放宽品类过滤，只按关键词 + 预算匹配，避免明明有货却回答「没找到」。
        if not scored and query_tokens and category:
            scored = self._build_candidates(
                query_tokens,
                category=None,
                budget_min=budget_min,
                budget_max=budget_max,
                require_overlap=True,
            )
        # 兜底 2：关键词无命中时，返回满足品类/预算过滤的商品，支持「有什么推荐」类问题
        if not scored and query_tokens:
            scored = self._build_candidates(
                query_tokens,
                category=category,
                budget_min=budget_min,
                budget_max=budget_max,
                require_overlap=False,
            )

        scored.sort(key=lambda item: item[0], reverse=True)
        return [product for _, product in scored[:top_k]]
