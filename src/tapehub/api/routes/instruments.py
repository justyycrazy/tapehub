"""标的解析路由：宽松输入 → 标准代码候选（只读本地规则，不访问上游）。

与 bars/quotes 的边界：主链路仍只接受标准代码；本端点提供「先解析、
再查询」的旁路，多候选如实返回，不验证标的是否真实存在。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from tapehub.api.dependencies import get_instrument_resolve_service, get_request_id
from tapehub.api.schemas.responses import InstrumentResolveResponse, resolve_to_response
from tapehub.application.instrument_resolve_service import InstrumentResolveService
from tapehub.domain.models import AssetType

router = APIRouter(prefix="/v1", tags=["instruments"])


@router.get("/instruments/resolve", response_model=InstrumentResolveResponse)
async def resolve_instrument(
    service: Annotated[InstrumentResolveService, Depends(get_instrument_resolve_service)],
    request_id: Annotated[str, Depends(get_request_id)],
    q: Annotated[
        str,
        Query(
            min_length=1,
            max_length=64,
            description="宽松标的输入，如 600000 / 00700 / USDJPY / AAPL / 600000.SH",
        ),
    ],
    asset_type: Annotated[
        AssetType | None,
        Query(description="资产类型提示：收窄歧义候选（如 000001 的指数/个股双关）"),
    ] = None,
    exchange: Annotated[
        str | None,
        Query(
            max_length=16,
            description="交易所提示：如 NASDAQ（美股 ticker 交易所不可推导，缺省返回多候选）",
        ),
    ] = None,
) -> InstrumentResolveResponse:
    result = service.resolve(q, asset_type=asset_type, exchange=exchange)
    return resolve_to_response(result, request_id)
