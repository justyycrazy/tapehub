"""API 响应模型：能力目录、健康检查与历史 K 线。"""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from tapehub.api.schemas.common import UtcDatetime
from tapehub.application.capability_service import ProviderCapabilitiesView
from tapehub.domain.capabilities import Capability
from tapehub.domain.models import BarBatch, BarQuery


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    api_version: str
    timestamp: UtcDatetime


class IntervalHistoryOut(BaseModel):
    start: UtcDatetime | None = None
    end: UtcDatetime | None = None


class IntervalEntryOut(BaseModel):
    code: str
    history: IntervalHistoryOut


class CapabilityOut(BaseModel):
    data_type: str
    markets: list[str]
    asset_types: list[str]
    delivery: list[str]
    intervals: list[IntervalEntryOut]
    adjustments: list[str]
    fields: list[str]


class ProviderCapabilitiesOut(BaseModel):
    provider: str
    enabled: bool
    runtime_status: str
    capabilities: list[CapabilityOut]


class CapabilitiesMeta(BaseModel):
    request_id: str
    generated_at: UtcDatetime
    catalog_version: str


class CapabilitiesResponse(BaseModel):
    data: list[ProviderCapabilitiesOut]
    meta: CapabilitiesMeta


class ProviderCapabilitiesResponse(BaseModel):
    data: ProviderCapabilitiesOut
    meta: CapabilitiesMeta


class BarOut(BaseModel):
    timestamp: UtcDatetime
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
    source_time: UtcDatetime | None = None
    received_at: UtcDatetime
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
        intervals=[
            IntervalEntryOut(
                code=entry.code.code,
                history=IntervalHistoryOut(start=entry.history.start, end=entry.history.end),
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
        capabilities=[capability_to_out(cap) for cap in view.capabilities],
    )


def bars_to_response(batch: BarBatch, query: BarQuery, request_id: str) -> BarsResponse:
    data = BarsDataOut(
        instrument=batch.instrument.code,
        asset_type=query.asset_type.value,
        interval=batch.interval.code,
        bars=[BarOut(**bar.model_dump()) for bar in batch.bars],
    )
    meta = BarsMeta(
        request_id=request_id,
        provider=batch.provider,
        source_time=batch.source_time,
        received_at=batch.received_at,
        offset=batch.offset,
        limit=batch.limit,
        count=batch.count,
        partial=batch.partial,
    )
    return BarsResponse(data=data, meta=meta, warnings=[])
