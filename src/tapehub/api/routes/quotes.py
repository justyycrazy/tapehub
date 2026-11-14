"""实时快照查询与流式路由。

API 层只做参数校验与响应组装；
Provider 选择、内部 operation 路由、订阅与首包等待都在下层完成。
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from tapehub.api.dependencies import (
    get_quote_query_service,
    get_quote_stream_service,
    get_request_id,
)
from tapehub.api.schemas.requests import QuotesQueryParams, to_quote_query
from tapehub.api.schemas.responses import QuoteResponse, quote_stream_frame, quote_to_response
from tapehub.application.quote_query_service import QuoteQueryService
from tapehub.application.quote_stream_service import QuoteStreamService

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


@router.get("/market/quotes/stream")
async def stream_market_quote(
    params: Annotated[QuotesQueryParams, Query()],
    service: Annotated[QuoteStreamService, Depends(get_quote_stream_service)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> StreamingResponse:
    """SSE 行情流：``text/event-stream``（quote / status / heartbeat 三类帧）。

    受理阶段（响应头发出前）的错误统一走 JSON 错误结构（含
    ``STREAM_LIMIT_REACHED`` 429）；流中事件见 docs/api.md「实时行情流（SSE）」。
    """
    query = to_quote_query(params)
    stream = service.open(query)  # 急切受理：闸门/路由错误在此抛出

    async def sse_frames() -> AsyncIterator[str]:
        # aclosing 保证「受理后响应体未消费即被丢弃」时仍走 aclose 释放
        # 配额（迭代器内部另有 cancel-await 与 GC 双兜底）
        async with contextlib.aclosing(stream.events):
            async for event in stream.events:
                yield quote_stream_frame(event, stream, request_id)

    return StreamingResponse(
        sse_frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 代理（如 nginx）不得缓冲 SSE；连接时长即「耗时」，见 docs/api.md
            "X-Accel-Buffering": "no",
        },
    )
