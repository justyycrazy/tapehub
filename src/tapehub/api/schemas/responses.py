"""API 响应模型：能力目录、健康检查、历史 K 线、实时快照与行情流。

时间字段分两类：

- ``BeijingDatetime``：服务级时间戳（健康检查 timestamp、能力目录 generated_at），
  项目内部统一北京时间（+08:00）；
- ``MarketDatetime``：行情数据时间（bar timestamp、received_at 等），
  已由领域层全市场统一归一化为北京时间（+08:00，2026-11-04 数据探针
  确认上游口径后统一），序列化原样保留偏移。
"""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from tapehub.api.schemas.common import BeijingDatetime, MarketDatetime
from tapehub.application.capability_service import ProviderCapabilitiesView
from tapehub.application.quote_stream_service import QuoteStream
from tapehub.domain.capabilities import Capability
from tapehub.domain.instrument_resolver import ResolveResult
from tapehub.domain.models import (
    CN_TIMEZONE,
    BarBatch,
    BarQuery,
    Quote,
    QuoteQuery,
    QuoteStreamEvent,
)


def beijing_now() -> datetime:
    """当前时刻（项目统一北京时间）。"""
    return datetime.now(CN_TIMEZONE)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    api_version: str
    timestamp: BeijingDatetime


class IntervalHistoryOut(BaseModel):
    start: MarketDatetime | None = None
    end: MarketDatetime | None = None
    # 滚动保留天数：覆盖「now − N 天 → now」，与 start 互斥（声明二选一）
    retention_days: int | None = None


class IntervalEntryOut(BaseModel):
    code: str
    history: IntervalHistoryOut


class CapabilityOut(BaseModel):
    data_type: str
    markets: list[str]
    asset_types: list[str]
    delivery: list[str]
    variants: list[str]
    intervals: list[IntervalEntryOut]
    adjustments: list[str]
    fields: list[str]


class ProviderCapabilitiesOut(BaseModel):
    provider: str
    enabled: bool
    runtime_status: str
    # 「Provider × 资产类型」级缺省变体：variant 缺省时的解析依据
    default_variant: dict[str, str]
    capabilities: list[CapabilityOut]


class CapabilitiesMeta(BaseModel):
    request_id: str
    generated_at: BeijingDatetime
    catalog_version: str


class CapabilitiesResponse(BaseModel):
    data: list[ProviderCapabilitiesOut]
    meta: CapabilitiesMeta


class ProviderCapabilitiesResponse(BaseModel):
    data: ProviderCapabilitiesOut
    meta: CapabilitiesMeta


class InstrumentCandidateOut(BaseModel):
    """单个解析候选。``asset_type`` 为 null 表示无法由输入格式判定，
    查询 bars/quotes 时仍由调用方显式传参。"""

    instrument: str
    market: str
    exchange: str
    symbol: str
    asset_type: str | None = None


class InstrumentResolveDataOut(BaseModel):
    query: str
    # 多于一个候选即歧义（如 000001 的指数/个股双关），由调用方以提示收窄
    ambiguous: bool
    candidates: list[InstrumentCandidateOut]


class InstrumentResolveMeta(BaseModel):
    request_id: str
    generated_at: BeijingDatetime


class InstrumentResolveResponse(BaseModel):
    data: InstrumentResolveDataOut
    meta: InstrumentResolveMeta


def resolve_to_response(result: ResolveResult, request_id: str) -> InstrumentResolveResponse:
    return InstrumentResolveResponse(
        data=InstrumentResolveDataOut(
            query=result.query,
            ambiguous=result.ambiguous,
            candidates=[
                InstrumentCandidateOut(
                    instrument=candidate.instrument.code,
                    market=candidate.instrument.market,
                    exchange=candidate.instrument.exchange,
                    symbol=candidate.instrument.symbol,
                    asset_type=(
                        candidate.asset_type.value if candidate.asset_type else None
                    ),
                )
                for candidate in result.candidates
            ],
        ),
        meta=InstrumentResolveMeta(
            request_id=request_id,
            generated_at=beijing_now(),
        ),
    )


class BarOut(BaseModel):
    timestamp: MarketDatetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    amount: float | None = None
    extensions: dict[str, Any] | None = None


class BarsDataOut(BaseModel):
    instrument: str
    asset_type: str
    interval: str
    bars: list[BarOut]


class BarsMeta(BaseModel):
    request_id: str
    provider: str
    # 实际使用的数据源变体：缺省请求经 default_variant 具体化后的值
    variant: str
    source_time: MarketDatetime | None = None
    received_at: MarketDatetime
    offset: int
    limit: int
    count: int
    partial: bool


class BarsResponse(BaseModel):
    data: BarsDataOut
    meta: BarsMeta
    warnings: list[str] = []


def capability_to_out(cap: Capability) -> CapabilityOut:
    return CapabilityOut(
        data_type=cap.data_type.value,
        markets=list(cap.markets),
        asset_types=[item.value for item in cap.asset_types],
        delivery=[item.value for item in cap.delivery],
        variants=[item.value for item in cap.variants],
        intervals=[
            IntervalEntryOut(
                code=entry.code.code,
                history=IntervalHistoryOut(
                    start=entry.history.start,
                    end=entry.history.end,
                    retention_days=entry.history.retention_days,
                ),
            )
            for entry in cap.intervals
        ],
        adjustments=[item.value for item in cap.adjustments],
        fields=list(cap.fields),
    )


def provider_view_to_out(view: ProviderCapabilitiesView) -> ProviderCapabilitiesOut:
    return ProviderCapabilitiesOut(
        provider=view.name,
        enabled=view.enabled,
        runtime_status=view.runtime_status,
        default_variant={
            asset_type.value: variant.value
            for asset_type, variant in view.default_variants.items()
        },
        capabilities=[capability_to_out(cap) for cap in view.capabilities],
    )


def bars_to_response(batch: BarBatch, query: BarQuery, request_id: str) -> BarsResponse:
    if query.variant is None:  # 不可达：BarQueryService 已具体化变体
        raise ValueError("query variant must be resolved before building the response")
    data = BarsDataOut(
        instrument=batch.instrument.code,
        asset_type=query.asset_type.value,
        interval=batch.interval.code,
        bars=[BarOut(**bar.model_dump()) for bar in batch.bars],
    )
    meta = BarsMeta(
        request_id=request_id,
        provider=batch.provider,
        variant=query.variant.value,
        source_time=batch.source_time,
        received_at=batch.received_at,
        offset=batch.offset,
        limit=batch.limit,
        count=batch.count,
        partial=batch.partial,
    )
    warnings: list[str] = []
    if batch.partial:
        warnings.append(
            "upstream returned the maximum number of bars; result may be truncated"
        )
    return BarsResponse(data=data, meta=meta, warnings=warnings)


class QuoteOut(BaseModel):
    """单条实时快照（行情时间为市场口径，已统一北京时间）。"""

    timestamp: MarketDatetime
    last_price: float | None = None
    bid_price1: float | None = None
    bid_volume1: float | None = None
    bid_price2: float | None = None
    bid_volume2: float | None = None
    bid_price3: float | None = None
    bid_volume3: float | None = None
    bid_price4: float | None = None
    bid_volume4: float | None = None
    bid_price5: float | None = None
    bid_volume5: float | None = None
    ask_price1: float | None = None
    ask_volume1: float | None = None
    ask_price2: float | None = None
    ask_volume2: float | None = None
    ask_price3: float | None = None
    ask_volume3: float | None = None
    ask_price4: float | None = None
    ask_volume4: float | None = None
    ask_price5: float | None = None
    ask_volume5: float | None = None
    volume: float | None = None
    amount: float | None = None
    extensions: dict[str, Any] | None = None


class QuoteDataOut(BaseModel):
    instrument: str
    asset_type: str
    quote: QuoteOut


class QuoteMeta(BaseModel):
    request_id: str
    provider: str
    # 实际使用的数据源变体：缺省请求经 default_variant 具体化后的值
    variant: str
    received_at: MarketDatetime


class QuoteResponse(BaseModel):
    data: QuoteDataOut
    meta: QuoteMeta


def quote_to_response(quote: Quote, query: QuoteQuery, request_id: str) -> QuoteResponse:
    if query.variant is None:  # 不可达：QuoteQueryService 已具体化变体
        raise ValueError("query variant must be resolved before building the response")
    data = QuoteDataOut(
        instrument=query.instrument.code,
        asset_type=query.asset_type.value,
        # provider/received_at 属于 meta（同 bars 链路），不进 data.quote
        quote=QuoteOut(**quote.model_dump(exclude={"provider", "received_at"})),
    )
    meta = QuoteMeta(
        request_id=request_id,
        provider=quote.provider,
        variant=query.variant.value,
        received_at=quote.received_at,
    )
    return QuoteResponse(data=data, meta=meta)


def quote_stream_frame(
    event: QuoteStreamEvent, stream: "QuoteStream", request_id: str
) -> str:
    """行情流事件 → SSE 帧（``event`` + ``data`` 两行，空行结尾）。

    - ``event: quote``：``data`` 与快照响应同构（instrument/asset_type/quote
      + provider/variant/received_at），客户端可复用同一解析逻辑；
    - ``event: status``：``data.status`` 取值见领域 ``StreamStatusKind``；
    - ``event: heartbeat``：空闲保活帧，``data.ts`` 为北京时间。
    """
    query = stream.query
    if query.variant is None:  # 不可达：QuoteStreamService 已具体化变体
        raise ValueError("query variant must be resolved before building the frame")
    if event.heartbeat:
        payload: dict[str, Any] = {
            "type": "heartbeat",
            "request_id": request_id,
            "ts": beijing_now().isoformat(),
        }
        return _sse_frame("heartbeat", payload)
    if event.quote is not None:
        payload = {
            "instrument": query.instrument.code,
            "asset_type": query.asset_type.value,
            "quote": QuoteOut(
                **event.quote.model_dump(exclude={"provider", "received_at"})
            ).model_dump(mode="json"),
            "provider": event.quote.provider,
            "variant": query.variant.value,
            "received_at": event.quote.received_at.isoformat(),
        }
        return _sse_frame("quote", payload)
    payload = {
        "status": event.status.value,
        "instrument": query.instrument.code,
        "asset_type": query.asset_type.value,
        "provider": stream.provider,
    }
    return _sse_frame("status", payload)


def _sse_frame(event: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n"
