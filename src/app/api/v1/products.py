"""商品管理 API：商品的增删改查。

商品是电商导购智能体的基础数据，本模块提供商品的创建、分页查询、详情、
更新与删除，为后续购物车、下单等交易能力提供数据支撑。
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from ...core.db.database import local_session
from ...core.db.models import Product

router = APIRouter(prefix="/products", tags=["商品模块"])


class ProductCreate(BaseModel):
    """创建商品请求体。"""

    name: str = Field(..., min_length=1, max_length=255, description="商品名称")
    category: str = Field(..., min_length=1, max_length=100, description="商品品类")
    description: str = Field(..., min_length=1, description="商品描述")
    price: float = Field(..., ge=0, description="商品价格（元），不能为负")
    embedding: list[float] | None = Field(None, description="商品向量（可选，供向量检索使用）")


class ProductUpdate(BaseModel):
    """更新商品请求体，仅提交需要修改的字段。"""

    name: str | None = Field(None, min_length=1, max_length=255)
    category: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, min_length=1)
    price: float | None = Field(None, ge=0)


class ProductResponse(BaseModel):
    """商品响应体（不含向量，避免返回过大的 embedding）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: str
    description: str
    price: float
    created_at: datetime
    updated_at: datetime | None


class ProductListResponse(BaseModel):
    """商品分页列表响应体。"""

    items: list[ProductResponse]
    total: int
    page: int
    page_size: int


@router.post("", response_model=ProductResponse, status_code=201, summary="创建商品")
async def create_product(request: ProductCreate) -> ProductResponse:
    """创建一个新商品。"""
    async with local_session() as session:
        product = Product(
            name=request.name,
            category=request.category,
            description=request.description,
            price=request.price,
            embedding=request.embedding,
        )
        session.add(product)
        await session.commit()
        await session.refresh(product)
        return ProductResponse.model_validate(product)


@router.get("", response_model=ProductListResponse, summary="分页查询商品")
async def list_products(
    category: str | None = Query(None, description="按品类过滤"),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
) -> ProductListResponse:
    """分页查询商品，可按品类过滤。"""
    conditions = []
    if category:
        conditions.append(Product.category == category)

    async with local_session() as session:
        count_stmt = select(func.count()).select_from(Product)
        list_stmt = select(Product).order_by(Product.id)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
            list_stmt = list_stmt.where(*conditions)
        total = await session.scalar(count_stmt)
        list_stmt = list_stmt.offset((page - 1) * page_size).limit(page_size)
        items = (await session.scalars(list_stmt)).all()
        return ProductListResponse(
            items=[ProductResponse.model_validate(item) for item in items],
            total=total or 0,
            page=page,
            page_size=page_size,
        )


@router.get("/{product_id}", response_model=ProductResponse, summary="查询商品详情")
async def get_product(product_id: int) -> ProductResponse:
    """按 id 查询单个商品。"""
    async with local_session() as session:
        product = await session.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="商品不存在")
        return ProductResponse.model_validate(product)


@router.patch("/{product_id}", response_model=ProductResponse, summary="更新商品")
async def update_product(product_id: int, request: ProductUpdate) -> ProductResponse:
    """按 id 更新商品，仅更新请求体中提交的字段。"""
    async with local_session() as session:
        product = await session.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="商品不存在")
        for field, value in request.model_dump(exclude_unset=True).items():
            setattr(product, field, value)
        await session.commit()
        await session.refresh(product)
        return ProductResponse.model_validate(product)


@router.delete("/{product_id}", status_code=204, summary="删除商品")
async def delete_product(product_id: int) -> None:
    """按 id 删除商品。"""
    async with local_session() as session:
        product = await session.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="商品不存在")
        await session.delete(product)
        await session.commit()
