from fastapi import APIRouter

from app.api.v1.announcement import router as announcement_router

from . import milvus
from .agent_evaluation import router as agent_evaluation_router
from .auth import router as auth_router
from .bi import router as bi_router
from .dingtalk import router as dingtalk_router
from .ecommerce import router as ecommerce_router
from .ecommerce_voice import router as ecommerce_voice_router
from .observability import router as observability_router
from .orchestrator import router as orchestrator_router
from .products import router as products_router
from .qweather import router as qweather_router
from .rag_evaluation import router as rag_evaluation_router
from .reflection import router as reflection_router

router = APIRouter(prefix="/v1")


@router.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """存活探针，用于快速验证服务是否正常启动。"""
    return {"status": "ok"}


router.include_router(milvus.router)
router.include_router(qweather_router)
router.include_router(dingtalk_router)
router.include_router(auth_router)
router.include_router(products_router)
router.include_router(ecommerce_router)
router.include_router(ecommerce_voice_router)
router.include_router(orchestrator_router)
router.include_router(bi_router)
router.include_router(observability_router)
router.include_router(agent_evaluation_router)
router.include_router(announcement_router)
router.include_router(rag_evaluation_router)
router.include_router(reflection_router)

# 在这里按业务模块注册 v1 路由，例如：
# from . import health
# router.include_router(health.router)
