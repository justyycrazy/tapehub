"""API 响应模型：能力目录、健康检查与历史 K 线。

时间字段分两类：

- ``BeijingDatetime``：服务级时间戳（健康检查 timestamp、能力目录 generated_at），
  项目内部统一北京时间（+08:00）；
- ``MarketDatetime``：行情数据时间（bar timestamp、received_at 等），
  已由领域层全市场统一归一化为北京时间（+08:00，2026-11-04 数据探针
  确认上游口径后统一），序列化原样保留偏移。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from tapehub.api.schemas.common import BeijingDatetime, MarketDatetime
from tapehub.application.capability_service import ProviderCapabilitiesView
from tapehub.domain.capabilities import Capability
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery


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
    # 声明为缺省数据源的资产类型：auto 路由优先级的依据（排序在前）
    default_provider: list[str]
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
        default_provider=[item.value for item in view.default_provider],
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
