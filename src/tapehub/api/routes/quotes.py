"""实时快照查询路由。

API 层只做参数校验与响应组装；
Provider 选择、内部 operation 路由、订阅与首包等待都在下层完成。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from tapehub.api.dependencies import get_quote_query_service, get_request_id
from tapehub.api.schemas.requests import QuotesQueryParams, to_quote_query
from tapehub.api.schemas.responses import QuoteResponse, quote_to_response
from tapehub.application.quote_query_service import QuoteQueryService

router = APIRouter(prefix="/v1", tags=["market"])


@router.get("/market/quotes", response_model=QuoteResponse)
async def get_market_quote(
    params: Annotated[QuotesQueryParams, Query()],
    service: Annotated[QuoteQueryService, Depends(get_quote_query_service)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> QuoteResponse:
    query = to_quote_query(params)
    result = await service.query(query)
    return quote_to_response(result.quote, result.query, request_id)
