"""QuoteStreamService（StreamHub）单元测试：流式通用语义，Provider 无关。

用受控脚本的 stub 适配器驱动（不依赖任何真实/假 SDK）：
容量闸门、心跳帧、空闲退订、优雅停机、流中错误事件化、
variant 具体化与配额释放。
"""

import asyncio
from dataclasses import dataclass

import pytest

from tapehub.application.quote_stream_service import QuoteStreamService
from tapehub.domain.errors import (
    StreamLimitReachedError,
    UnsupportedCapabilityError,
    UpstreamError,
)
from tapehub.domain.models import (
    CN_TIMEZONE,
    AssetType,
    DataVariant,
    Instrument,
    Quote,
    QuoteQuery,
    QuoteStreamEvent,
    StreamStatusKind,
)
from tapehub.providers.registry import ProviderRecord, ResolvedCandidate

_SYMBOL = "CN-SSE-600000"


def _quote_event(last_price: float) -> QuoteStreamEvent:
    from datetime import datetime

    return QuoteStreamEvent(
        quote=Quote(
            timestamp=datetime.now(CN_TIMEZONE),
            last_price=last_price,
            provider="stub-provider",
            received_at=datetime.now(CN_TIMEZONE),
        )
    )


class _ScriptAdapter:
    """脚本化流适配器：按脚本产事件（Exception 抛出），``hang`` 时末尾永挂。"""

    def __init__(self, script: list | None = None, *, hang: bool = False) -> None:
        self.queries: list[QuoteQuery] = []
        self._script = list(script or [])
        self._hang = hang

    def stream_quote(self, query: QuoteQuery):
        self.queries.append(query)

        async def _gen():
            for item in self._script:
                if isinstance(item, Exception):
                    raise item
                yield item
            if self._hang:
                await asyncio.Event().wait()

        return _gen()


@dataclass
class _StubRouter:
    """最小路由 stub：恒返回同一适配器候选。"""

    adapter: object
    variant: DataVariant = DataVariant.TQSDK

    def select_quote(self, query: QuoteQuery) -> ResolvedCandidate:
        record = ProviderRecord(name="stub-provider", manifest=object())
        record.adapter = self.adapter
        return ResolvedCandidate(record=record, variant=self.variant)


def _query(**overrides: object) -> QuoteQuery:
    base: dict = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse(_SYMBOL),
    }
    base.update(overrides)
    return QuoteQuery(**base)


def _service(adapter: object, **kwargs: object) -> QuoteStreamService:
    options: dict = {
        "max_clients": 2,
        "heartbeat_seconds": 0.05,
        "idle_timeout_seconds": 60.0,
    }
    options.update(kwargs)
    return QuoteStreamService(_StubRouter(adapter), **options)  # type: ignore[arg-type]


async def _take(stream, count: int, timeout: float = 3.0) -> list[QuoteStreamEvent]:
    """消费前 count 个事件后停（async for 退出即 aclose）。"""
    out: list[QuoteStreamEvent] = []

    async def _run() -> None:
        async for event in stream.events:
            out.append(event)
            if len(out) >= count:
                break

    await asyncio.wait_for(_run(), timeout)
    return out


def test_open_materializes_variant_and_passes_query() -> None:
    adapter = _ScriptAdapter([_quote_event(1.0)])
    service = _service(adapter)
    stream = service.open(_query())  # variant 未表达
    assert stream.query.variant == DataVariant.TQSDK  # 已按路由结果具体化
    assert stream.provider == "stub-provider"
    asyncio.run(_take(stream, 1))
    assert adapter.queries[0].variant == DataVariant.TQSDK  # 适配器收到具体值


def test_client_limit_reached() -> None:
    adapter = _ScriptAdapter(
        [QuoteStreamEvent(status=StreamStatusKind.SUBSCRIBED)], hang=True
    )
    service = _service(adapter, max_clients=1)
    first = service.open(_query())
    with pytest.raises(StreamLimitReachedError) as excinfo:
        service.open(_query())
    assert excinfo.value.details["limit"] == 1
    asyncio.run(_close(first))
    # 配额释放后可再次受理
    service.open(_query())


async def _close(stream) -> None:
    await stream.events.aclose()


def test_heartbeat_frames_when_idle() -> None:
    # 首事件（订阅确认）后进入静默：心跳帧按周期出现
    adapter = _ScriptAdapter(
        [QuoteStreamEvent(status=StreamStatusKind.SUBSCRIBED)], hang=True
    )
    service = _service(adapter)
    stream = service.open(_query())
    events = asyncio.run(_take(stream, 3))
    assert events[0].status == StreamStatusKind.SUBSCRIBED
    assert all(event.heartbeat for event in events[1:])


def test_idle_timeout_unsubscribes() -> None:
    adapter = _ScriptAdapter(
        [QuoteStreamEvent(status=StreamStatusKind.SUBSCRIBED)], hang=True
    )
    service = _service(adapter, idle_timeout_seconds=0.12)
    stream = service.open(_query())
    events = asyncio.run(_take(stream, 5))
    # 心跳若干帧（不重置空闲计时）后 idle_unsubscribed 收尾
    assert events[-1].status == StreamStatusKind.IDLE_UNSUBSCRIBED
    assert any(event.heartbeat for event in events[:-1])


def test_shutdown_terminates_streams_explicitly() -> None:
    adapter = _ScriptAdapter(
        [QuoteStreamEvent(status=StreamStatusKind.SUBSCRIBED)], hang=True
    )
    service = _service(adapter)
    stream = service.open(_query())

    async def scenario() -> list[QuoteStreamEvent]:
        task = asyncio.ensure_future(_collect_all(stream))
        await asyncio.sleep(0.02)
        await service.shutdown()
        return await task

    events = asyncio.run(scenario())
    assert events[-1].status == StreamStatusKind.UPSTREAM_DISCONNECTED
    assert service.client_count == 0


async def _collect_all(stream) -> list[QuoteStreamEvent]:
    out: list[QuoteStreamEvent] = []
    async for event in stream.events:
        out.append(event)
    return out


def test_open_rejected_after_shutdown() -> None:
    service = _service(_ScriptAdapter())
    asyncio.run(service.shutdown())
    with pytest.raises(UnsupportedCapabilityError):
        service.open(_query())


def test_upstream_error_becomes_disconnect_event() -> None:
    adapter = _ScriptAdapter([UpstreamError(details={"provider": "stub-provider"})])
    service = _service(adapter)
    stream = service.open(_query())
    events = asyncio.run(_collect_all(stream))
    assert events == [QuoteStreamEvent(status=StreamStatusKind.UPSTREAM_DISCONNECTED)]
    assert service.client_count == 0


def test_adapter_without_streaming_rejected() -> None:
    class _BarsOnly:
        pass

    service = _service(_BarsOnly())
    with pytest.raises(UnsupportedCapabilityError):
        service.open(_query())
    assert service.client_count == 0


def test_close_releases_client_slot() -> None:
    adapter = _ScriptAdapter([_quote_event(1.0), _quote_event(2.0)])
    service = _service(adapter, max_clients=1)
    stream = service.open(_query())
    assert service.client_count == 1
    asyncio.run(_take(stream, 2))
    assert service.client_count == 0
