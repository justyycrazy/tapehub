"""能力查询路由：只读取本地能力目录，不实时访问上游。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from tapehub.api.dependencies import get_capability_service, get_request_id, get_settings
from tapehub.api.schemas.responses import (
    CapabilitiesMeta,
    CapabilitiesResponse,
    ProviderCapabilitiesResponse,
    provider_view_to_out,
    utc_now,
)
from tapehub.application.capability_service import CapabilityService, parse_capability_filters
from tapehub.domain.models import AssetType, DataType, DataVariant
from tapehub.settings import Settings

router = APIRouter(prefix="/v1", tags=["capabilities"])


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def list_capabilities(
    request: Request,
    service: Annotated[CapabilityService, Depends(get_capability_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    request_id: Annotated[str, Depends(get_request_id)],
    provider: Annotated[str | None, Query(description="按 provider 名精确过滤")] = None,
    data_type: Annotated[DataType | None, Query(description="按数据类型过滤")] = None,
    market: Annotated[str | None, Query(description="按市场过滤，如 CN")] = None,
    asset_type: Annotated[AssetType | None, Query(description="按资产类型过滤")] = None,
    interval: Annotated[str | None, Query(description="按 ISO 8601 周期过滤，如 P1D")] = None,
    variant: Annotated[
        DataVariant | None, Query(description="按数据源变体过滤，如 eastmoney")
    ] = None,
) -> CapabilitiesResponse:
    interval_value = parse_capability_filters(interval=interval)
    views = service.list_providers(
        provider=provider,
        data_type=data_type,
        market=market,
        asset_type=asset_type,
        interval=interval_value,
        variant=variant,
    )
    return CapabilitiesResponse(
        data=[provider_view_to_out(view) for view in views],
        meta=CapabilitiesMeta(
            request_id=request_id,
            generated_at=utc_now(),
            catalog_version=settings.catalog_version,
        ),
    )


@router.get(
    "/providers/{provider}/capabilities",
    response_model=ProviderCapabilitiesResponse,
)
async def get_provider_capabilities(
    provider: str,
    request: Request,
    service: Annotated[CapabilityService, Depends(get_capability_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> ProviderCapabilitiesResponse:
    view = service.get_provider(provider)
    return ProviderCapabilitiesResponse(
        data=provider_view_to_out(view),
        meta=CapabilitiesMeta(
            request_id=request_id,
            generated_at=utc_now(),
            catalog_version=settings.catalog_version,
        ),
    )
