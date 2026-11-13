"""查询应用服务的上游整体超时测试（M2，2026-11-13 审计修复）。

覆盖点：``upstream_timeout_seconds`` > 0 时 ``asyncio.wait_for`` 超时 →
既有 ``TimeoutError`` 映射转 ``UPSTREAM_TIMEOUT``（504）；= 0 时不包裹；
QuoteQueryService 与 BarQueryService 共享同一包装逻辑。
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tapehub.application.bar_query_service import BarQueryService
from tapehub.application.provider_router import ProviderRouter
from tapehub.application.quote_query_service import QuoteQueryService
from tapehub.domain.errors import UpstreamTimeoutError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import (
    Adjust,
    AssetType,
    BarQuery,
    DataVariant,
    Instrument,
    QuoteQuery,
)
from tapehub.providers.manifest import load_manifest_file
from tapehub.providers.registry import ProviderRegistry, build_provider_record
from tests.conftest import fake_routing
from tests.fixtures.providers.fake_provider import FakeBarProvider

FIXTURE_MANIFEST = Path(__file__).parents[1] / "fixtures" / "manifests" / "fake_provider.yaml"

CN = timezone.utc  # Bar 只要求时区感知，偏移不限


class _SlowProvider(FakeBarProvider):
    """取数前睡 0.3s 的 Fake 适配器：超时窗口的可控慢上游。"""

    async def fetch_bars(self, query: BarQuery):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.3)
        return await super().fetch_bars(query)

    async def fetch_quote(self, query: QuoteQuery):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.3)
        raise AssertionError("quote fast path not expected in timeout tests")


def _router() -> ProviderRouter:
    manifest = load_manifest_file(FIXTURE_MANIFEST)
    record = build_provider_record("fake-provider", manifest, _SlowProvider)
    registry = ProviderRegistry([record], fake_routing())
    return ProviderRouter(registry)


def _bar_query() -> BarQuery:
    return BarQuery(
        asset_type=AssetType.STOCK,
        instrument=Instrument.parse("CN-SSE-600000"),
        interval=Interval.parse("P1D"),
        start=datetime(2025, 1, 1, tzinfo=CN),
        end=datetime(2025, 1, 5, tzinfo=CN),
        adjust=Adjust.NONE,
        variant=DataVariant.EASTMONEY,
    )


def test_bar_query_timeout_raises_upstream_timeout() -> None:
    """超时窗口（0.05s）小于上游耗时（0.3s）→ UPSTREAM_TIMEOUT（504 语义）。"""
    service = BarQueryService(_router(), upstream_timeout_seconds=0.05)
    with pytest.raises(UpstreamTimeoutError):
        asyncio.run(service.query(_bar_query()))


def test_bar_query_timeout_disabled_passes_through() -> None:
    """timeout=0 不包裹：慢上游跑完返回（显式关闭语义）。"""
    service = BarQueryService(_router(), upstream_timeout_seconds=0)
    result = asyncio.run(service.query(_bar_query()))
    assert result.batch.count > 0


def test_bar_query_within_timeout_returns_normally() -> None:
    """上游在窗口内完成 → 正常返回（超时机制不误伤快路径）。"""
    service = BarQueryService(_router(), upstream_timeout_seconds=5.0)
    result = asyncio.run(service.query(_bar_query()))
    assert result.batch.count > 0


def test_quote_query_timeout_wrapper() -> None:
    """QuoteQueryService 与 Bar 服务共享包装逻辑（直测包装方法，路由无关）。"""
    service = QuoteQueryService(_router(), upstream_timeout_seconds=0.05)

    class _SlowQuote:
        async def fetch_quote(self, query: QuoteQuery) -> object:
            await asyncio.sleep(0.3)
            raise AssertionError("should not complete")

    async def scenario() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await service._fetch_with_timeout(
                _SlowQuote(),
                QuoteQuery(
                    asset_type=AssetType.STOCK,
                    instrument=Instrument.parse("CN-SSE-600000"),
                    variant=DataVariant.EASTMONEY,
                ),
            )

    asyncio.run(scenario())
