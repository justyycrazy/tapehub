"""历史 K 线查询路由。

API 层只做参数校验与响应组装；
Provider 选择、内部 operation 路由与数据转换都在下层完成。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from tapehub.api.dependencies import get_bar_query_service, get_request_id, get_settings
from tapehub.api.schemas.requests import BarsQueryParams, to_bar_query
from tapehub.api.schemas.responses import BarsResponse, bars_to_response
from tapehub.application.bar_query_service import BarQueryService
from tapehub.settings import Settings

router = APIRouter(prefix="/v1", tags=["market"])


@router.get("/market/bars", response_model=BarsResponse)
async def get_market_bars(
    params: Annotated[BarsQueryParams, Query()],
    service: Annotated[BarQueryService, Depends(get_bar_query_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> BarsResponse:
    query = to_bar_query(params, settings)
    batch = await service.query(query)
    return bars_to_response(batch, query, request_id)
