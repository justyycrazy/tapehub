"""健康检查：固定路径、无鉴权、不访问任何上游 Provider。"""

from fastapi import APIRouter, Depends

from tapehub.api.dependencies import get_settings
from tapehub.api.schemas.responses import HealthResponse, utc_now
from tapehub.settings import Settings

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """供其他项目配置 base_url 后测试连接。

    其他项目可同时校验 HTTP 200 与 ``service == "tapehub"``，
    避免 base_url 配置到了其他服务。
    """
    return HealthResponse(
        status="ok",
        service="tapehub",
        version=settings.version,
        api_version=settings.api_version,
        timestamp=utc_now(),
    )
