"""把内置商品目录写入 products 表，供智能问数演示使用。

幂等：products 表已有数据时直接跳过，可重复执行。

用法（项目根目录）：

    uv run python src/scripts/seed_demo_products.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlalchemy import func, select  # noqa: E402

from app.core.agents.ecommerce.product.builtin import _CATALOG  # noqa: E402
from app.core.db.database import Base, async_engine, local_session  # noqa: E402
from app.core.db.models import Product  # noqa: E402


async def main() -> None:
    """如果 products 为空，则写入内置商品；否则跳过。"""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with local_session() as session:
        existing = await session.scalar(select(func.count()).select_from(Product))
        if existing:
            print(f"products 已有 {existing} 行，跳过写入。")
            return
        for _pid, name, category, description, price in _CATALOG:
            session.add(Product(name=name, category=category, description=description, price=price, embedding=None))
        await session.commit()
        print(f"已写入 {len(_CATALOG)} 条内置商品。")


if __name__ == "__main__":
    asyncio.run(main())
